# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""This module contains all of the core logic for beets' command-line
interface. To invoke the CLI, just call beets.ui.main(). The actual
CLI commands are implemented in the ui.commands module.
"""

from __future__ import annotations

import errno
import optparse
import os.path
import re
import sqlite3
import sys
import textwrap
import traceback
import warnings
from typing import Any, Callable

import confuse
from rich.logging import RichHandler
from rich.traceback import install

from beets import config, library, logging, plugins, util
from beets.dbcore import db
from beets.dbcore import query as db_query
from beets.util import as_string, colordiff, colorize, get_console
from beets.util.functemplate import template

# On Windows platforms, use colorama to support "ANSI" terminal colors.
if sys.platform == "win32":
    try:
        import colorama
    except ImportError:
        pass
    else:
        colorama.init()

log = logging.getLogger(__name__)


class SafeRichHandler(RichHandler):
    def emit(self, record) -> None:
        try:
            return super().emit(record)
        except Exception:
            self.handleError(record)


PF_KEY_QUERIES = {
    "comp": "comp:true",
    "singleton": "singleton:true",
}


class UserError(Exception):
    """UI exception. Commands should throw this in order to display
    nonrecoverable errors to the user.
    """


# Encoding utilities.


def _in_encoding():
    """Get the encoding to use for *inputting* strings from the console."""
    return _stream_encoding(sys.stdin)


def _out_encoding():
    """Get the encoding to use for *outputting* strings to the console."""
    return _stream_encoding(sys.stdout)


def _stream_encoding(stream, default="utf-8"):
    """A helper for `_in_encoding` and `_out_encoding`: get the stream's
    preferred encoding, using a configured override or a default
    fallback if neither is not specified.
    """
    # Configured override?
    encoding = config["terminal_encoding"].get()
    if encoding:
        return encoding

    # For testing: When sys.stdout or sys.stdin is a StringIO under the
    # test harness, it doesn't have an `encoding` attribute. Just use
    # UTF-8.
    if not hasattr(stream, "encoding"):
        return default

    # Python's guessed output stream encoding, or UTF-8 as a fallback
    # (e.g., when piped to a file).
    return stream.encoding or default


def decargs(arglist):
    """Given a list of command-line argument bytestrings, attempts to
    decode them to Unicode strings when running under Python 2.

    .. deprecated:: 2.4.0
       This function will be removed in 3.0.0.
    """
    warnings.warn(
        "decargs() is deprecated and will be removed in version 3.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return arglist


def print_(*strings: str, end: str = "\n") -> None:
    """Like print, but rather than raising an error when a character
    is not in the terminal's encoding's character set, just silently
    replaces it.

    The `end` keyword argument behaves similarly to the built-in `print`
    (it defaults to a newline).
    """
    get_console().print(" ".join(strings or []), end=end)


# Configuration wrappers.


def _bool_fallback(a, b):
    """Given a boolean or None, return the original value or a fallback."""
    if a is None:
        assert isinstance(b, bool)
        return b
    else:
        assert isinstance(a, bool)
        return a


def should_write(write_opt=None):
    """Decide whether a command that updates metadata should also write
    tags, using the importer configuration as the default.
    """
    return _bool_fallback(write_opt, config["import"]["write"].get(bool))


def should_move(move_opt=None):
    """Decide whether a command that updates metadata should also move
    files when they're inside the library, using the importer
    configuration as the default.

    Specifically, commands should move files after metadata updates only
    when the importer is configured *either* to move *or* to copy files.
    They should avoid moving files when the importer is configured not
    to touch any filenames.
    """
    return _bool_fallback(
        move_opt,
        config["import"]["move"].get(bool)
        or config["import"]["copy"].get(bool),
    )


# Input prompts.


def indent(count):
    """Returns a string with `count` many spaces."""
    return " " * count


def input_(prompt=None):
    """Like `input`, but decodes the result to a Unicode string.
    Raises a UserError if stdin is not available. The prompt is sent to
    stdout rather than stderr. A printed between the prompt and the
    input cursor.
    """
    # raw_input incorrectly sends prompts to stderr, not stdout, so we
    # use print_() explicitly to display prompts.
    # https://bugs.python.org/issue1927
    if prompt:
        print_(prompt, end=" ")

    try:
        resp = input()
    except EOFError:
        raise UserError("stdin stream ended while input required")

    return resp


def input_options(
    options,
    require=False,
    prompt=None,
    fallback_prompt=None,
    numrange=None,
    default=None,
    max_width=72,
):
    """Prompts a user for input. The sequence of `options` defines the
    choices the user has. A single-letter shortcut is inferred for each
    option; the user's choice is returned as that single, lower-case
    letter. The options should be provided as lower-case strings unless
    a particular shortcut is desired; in that case, only that letter
    should be capitalized.

    By default, the first option is the default. `default` can be provided to
    override this. If `require` is provided, then there is no default. The
    prompt and fallback prompt are also inferred but can be overridden.

    If numrange is provided, it is a pair of `(high, low)` (both ints)
    indicating that, in addition to `options`, the user may enter an
    integer in that inclusive range.

    `max_width` specifies the maximum number of columns in the
    automatically generated prompt string.
    """
    # Assign single letters to each option. Also capitalize the options
    # to indicate the letter.
    letters = {}
    display_letters = []
    capitalized = []
    first = True
    for option in options:
        # Is a letter already capitalized?
        for letter in option:
            if letter.isalpha() and letter.upper() == letter:
                found_letter = letter
                break
        else:
            # Infer a letter.
            for letter in option:
                if not letter.isalpha():
                    continue  # Don't use punctuation.
                if letter not in letters:
                    found_letter = letter
                    break
            else:
                raise ValueError("no unambiguous lettering found")

        letters[found_letter.lower()] = option
        index = option.index(found_letter)

        # Mark the option's shortcut letter for display.
        if not require and (
            (default is None and not numrange and first)
            or (
                isinstance(default, str)
                and found_letter.lower() == default.lower()
            )
        ):
            # The first option is the default; mark it.
            show_letter = f"[{found_letter.upper()}]"
            is_default = True
        else:
            show_letter = found_letter.upper()
            is_default = False

        # Colorize the letter shortcut.
        show_letter = colorize(
            "action_default" if is_default else "action", show_letter
        )

        # Insert the highlighted letter back into the word.
        descr_color = "action_default" if is_default else "action_description"
        capitalized.append(
            colorize(descr_color, option[:index])
            + show_letter
            + colorize(descr_color, option[index + 1 :])
        )
        display_letters.append(found_letter.upper())

        first = False

    # The default is just the first option if unspecified.
    if require:
        default = None
    elif default is None:
        if numrange:
            default = numrange[0]
        else:
            default = display_letters[0].lower()

    # Make a prompt if one is not provided.
    if not prompt:
        prompt_parts = []
        prompt_part_lengths = []
        if numrange:
            if isinstance(default, int):
                default_name = str(default)
                default_name = colorize("action_default", default_name)
                tmpl = "# selection (default {})"
                prompt_parts.append(tmpl.format(default_name))
                prompt_part_lengths.append(len(tmpl) - 2 + len(str(default)))
            else:
                prompt_parts.append("# selection")
                prompt_part_lengths.append(len(prompt_parts[-1]))
        prompt_parts += capitalized
        prompt_part_lengths += [len(s) for s in options]

        # Wrap the query text.
        # Start prompt with U+279C: Heavy Round-Tipped Rightwards Arrow
        prompt = colorize("action", "\u279c ")
        line_length = 0
        for i, (part, length) in enumerate(
            zip(prompt_parts, prompt_part_lengths)
        ):
            # Add punctuation.
            if i == len(prompt_parts) - 1:
                part += colorize("action_description", "?")
            else:
                part += colorize("action_description", ",")
            length += 1

            # Choose either the current line or the beginning of the next.
            if line_length + length + 1 > max_width:
                prompt += "\n"
                line_length = 0

            if line_length != 0:
                # Not the beginning of the line; need a space.
                part = f" {part}"
                length += 1

            prompt += part
            line_length += length

    # Make a fallback prompt too. This is displayed if the user enters
    # something that is not recognized.
    if not fallback_prompt:
        fallback_prompt = "Enter one of "
        if numrange:
            fallback_prompt += "{}-{}, ".format(*numrange)
        fallback_prompt += f"{', '.join(display_letters)}:"

    resp = input_(prompt)
    while True:
        resp = resp.strip().lower()

        # Try default option.
        if default is not None and not resp:
            resp = default

        # Try an integer input if available.
        if numrange:
            try:
                resp = int(resp)
            except ValueError:
                pass
            else:
                low, high = numrange
                if low <= resp <= high:
                    return resp
                else:
                    resp = None

        # Try a normal letter input.
        if resp:
            resp = resp[0]
            if resp in letters:
                return resp

        # Prompt for new input.
        resp = input_(fallback_prompt)


def input_yn(prompt, require=False):
    """Prompts the user for a "yes" or "no" response. The default is
    "yes" unless `require` is `True`, in which case there is no default.
    """
    # Start prompt with U+279C: Heavy Round-Tipped Rightwards Arrow
    yesno = colorize("action", "\u279c ") + colorize(
        "action_description", "Enter Y or N:"
    )
    sel = input_options(("y", "n"), require, prompt, yesno)
    return sel == "y"


def input_select_objects(prompt, objs, rep, prompt_all=None):
    """Prompt to user to choose all, none, or some of the given objects.
    Return the list of selected objects.

    `prompt` is the prompt string to use for each question (it should be
    phrased as an imperative verb). If `prompt_all` is given, it is used
    instead of `prompt` for the first (yes(/no/select) question.
    `rep` is a function to call on each object to print it out when confirming
    objects individually.
    """
    choice = input_options(
        ("y", "n", "s"), False, f"{prompt_all or prompt}? (Yes/no/select)"
    )
    print()  # Blank line.

    if choice == "y":  # Yes.
        return objs

    elif choice == "s":  # Select.
        out = []
        for obj in objs:
            rep(obj)
            answer = input_options(
                ("y", "n", "q"),
                True,
                f"{prompt}? (yes/no/quit)",
                "Enter Y or N:",
            )
            if answer == "y":
                out.append(obj)
            elif answer == "q":
                return out
        return out

    else:  # No.
        return []


def get_path_formats(subview=None):
    """Get the configuration's path formats as a list of query/template
    pairs.
    """
    path_formats = []
    subview = subview or config["paths"]
    for query, view in subview.items():
        query = PF_KEY_QUERIES.get(query, query)  # Expand common queries.
        path_formats.append((query, template(view.as_str())))
    return path_formats


def get_replacements():
    """Confuse validation function that reads regex/string pairs."""
    replacements = []
    for pattern, repl in config["replace"].get(dict).items():
        repl = repl or ""
        try:
            replacements.append((re.compile(pattern), repl))
        except re.error:
            raise UserError(
                f"malformed regular expression in replace: {pattern}"
            )
    return replacements


FLOAT_EPSILON = 0.01


def _field_diff(field, old, old_fmt, new, new_fmt):
    """Given two Model objects and their formatted views, format their values
    for `field` and highlight changes among them. Return a human-readable
    string. If the value has not changed, return None instead.
    """
    oldval = old.get(field)
    newval = new.get(field)

    # If no change, abort.
    if (
        isinstance(oldval, float)
        and isinstance(newval, float)
        and abs(oldval - newval) < FLOAT_EPSILON
    ):
        return None
    elif oldval == newval:
        return None

    # Get formatted values for output.
    oldval = old_fmt.get(field, "")
    newval = new_fmt.get(field, "")

    return colordiff(str(oldval), str(newval))


def show_model_changes(new, old=None, fields=None, always=False):
    """Given a Model object, print a list of changes from its pristine
    version stored in the database. Return a boolean indicating whether
    any changes were found.

    `old` may be the "original" object to avoid using the pristine
    version from the database. `fields` may be a list of fields to
    restrict the detection to. `always` indicates whether the object is
    always identified, regardless of whether any changes are present.
    """
    old = old or new._db._get(type(new), new.id)

    # Keep the formatted views around instead of re-creating them in each
    # iteration step
    old_fmt = old.formatted()
    new_fmt = new.formatted()

    # Build up lines showing changed fields.
    changes = []
    for field in old:
        # Subset of the fields. Never show mtime.
        if field == "mtime" or (fields and field not in fields):
            continue

        # Detect and show difference for this field.
        line = _field_diff(field, old, old_fmt, new, new_fmt)
        if line:
            changes.append(f"  {field}: {line}")

    # New fields.
    for field in set(new) - set(old):
        if fields and field not in fields:
            continue

        changes.append(
            f"  {field}: {colorize('text_highlight', new_fmt[field])}"
        )

    # Print changes.
    if changes or always:
        print_(format(old))
    if changes:
        print_("\n".join(changes))

    return bool(changes)


def show_path_changes(path_changes):
    """Given a list of tuples (source, destination) that indicate the
    path changes, log the changes as INFO-level output to the beets log.
    The output is guaranteed to be unicode.

    Every pair is shown on a single line if the terminal width permits it,
    else it is split over two lines. E.g.,

    Source -> Destination

    vs.

    Source ->
    Destination
    """
    sources, destinations = zip(*path_changes)

    # Ensure unicode output
    sources = list(map(util.displayable_path, sources))
    destinations = list(map(util.displayable_path, destinations))

    for source, dest in zip(sources, destinations):
        get_console().print(colordiff(source, dest), highlight=False)


# Helper functions for option parsing.


def _store_dict(option, opt_str, value, parser):
    """Custom action callback to parse options which have ``key=value``
    pairs as values. All such pairs passed for this option are
    aggregated into a dictionary.
    """
    dest = option.dest
    option_values = getattr(parser.values, dest, None)

    if option_values is None:
        # This is the first supplied ``key=value`` pair of option.
        # Initialize empty dictionary and get a reference to it.
        setattr(parser.values, dest, {})
        option_values = getattr(parser.values, dest)

    try:
        key, value = value.split("=", 1)
        if not (key and value):
            raise ValueError
    except ValueError:
        raise UserError(
            f"supplied argument `{value}' is not of the form `key=value'"
        )

    option_values[key] = value


class CommonOptionsParser(optparse.OptionParser):
    """Offers a simple way to add common formatting options.

    Options available include:
        - matching albums instead of tracks: add_album_option()
        - showing paths instead of items/albums: add_path_option()
        - changing the format of displayed items/albums: add_format_option()

    The last one can have several behaviors:
        - against a special target
        - with a certain format
        - autodetected target with the album option

    Each method is fully documented in the related method.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._album_flags = False
        # this serves both as an indicator that we offer the feature AND allows
        # us to check whether it has been specified on the CLI - bypassing the
        # fact that arguments may be in any order

    def add_album_option(self, flags=("-a", "--album")):
        """Add a -a/--album option to match albums instead of tracks.

        If used then the format option can auto-detect whether we're setting
        the format for items or albums.
        Sets the album property on the options extracted from the CLI.
        """
        album = optparse.Option(
            *flags, action="store_true", help="match albums instead of tracks"
        )
        self.add_option(album)
        self._album_flags = set(flags)

    def _set_format(
        self,
        option,
        opt_str,
        value,
        parser,
        target=None,
        fmt=None,
        store_true=False,
    ):
        """Internal callback that sets the correct format while parsing CLI
        arguments.
        """
        if store_true:
            setattr(parser.values, option.dest, True)

        # Use the explicitly specified format, or the string from the option.
        value = fmt or value or ""
        parser.values.format = value

        if target:
            config[target._format_config_key].set(value)
        else:
            if self._album_flags:
                if parser.values.album:
                    target = library.Album
                else:
                    # the option is either missing either not parsed yet
                    if self._album_flags & set(parser.rargs):
                        target = library.Album
                    else:
                        target = library.Item
                config[target._format_config_key].set(value)
            else:
                config[library.Item._format_config_key].set(value)
                config[library.Album._format_config_key].set(value)

    def add_path_option(self, flags=("-p", "--path")):
        """Add a -p/--path option to display the path instead of the default
        format.

        By default this affects both items and albums. If add_album_option()
        is used then the target will be autodetected.

        Sets the format property to '$path' on the options extracted from the
        CLI.
        """
        path = optparse.Option(
            *flags,
            nargs=0,
            action="callback",
            callback=self._set_format,
            callback_kwargs={"fmt": "$path", "store_true": True},
            help="print paths for matched items or albums",
        )
        self.add_option(path)

    def add_format_option(self, flags=("-f", "--format"), target=None):
        """Add -f/--format option to print some LibModel instances with a
        custom format.

        `target` is optional and can be one of ``library.Item``, 'item',
        ``library.Album`` and 'album'.

        Several behaviors are available:
            - if `target` is given then the format is only applied to that
            LibModel
            - if the album option is used then the target will be autodetected
            - otherwise the format is applied to both items and albums.

        Sets the format property on the options extracted from the CLI.
        """
        kwargs = {}
        if target:
            if isinstance(target, str):
                target = {"item": library.Item, "album": library.Album}[target]
            kwargs["target"] = target

        opt = optparse.Option(
            *flags,
            action="callback",
            callback=self._set_format,
            callback_kwargs=kwargs,
            help="print with custom format",
        )
        self.add_option(opt)

    def add_all_common_options(self):
        """Add album, path and format options."""
        self.add_album_option()
        self.add_path_option()
        self.add_format_option()


# Subcommand parsing infrastructure.
#
# This is a fairly generic subcommand parser for optparse. It is
# maintained externally here:
# https://gist.github.com/462717
# There you will also find a better description of the code and a more
# succinct example program.


class Subcommand:
    """A subcommand of a root command-line application that may be
    invoked by a SubcommandOptionParser.
    """

    func: Callable[[library.Library, optparse.Values, list[str]], Any]

    def __init__(self, name, parser=None, help="", aliases=(), hide=False):
        """Creates a new subcommand. name is the primary way to invoke
        the subcommand; aliases are alternate names. parser is an
        OptionParser responsible for parsing the subcommand's options.
        help is a short description of the command. If no parser is
        given, it defaults to a new, empty CommonOptionsParser.
        """
        self.name = name
        self.parser = parser or CommonOptionsParser()
        self.aliases = aliases
        self.help = help
        self.hide = hide
        self._root_parser = None

    def print_help(self):
        self.parser.print_help()

    def parse_args(self, args):
        return self.parser.parse_args(args)

    @property
    def root_parser(self):
        return self._root_parser

    @root_parser.setter
    def root_parser(self, root_parser):
        self._root_parser = root_parser
        self.parser.prog = (
            f"{as_string(root_parser.get_prog_name())} {self.name}"
        )


class SubcommandsOptionParser(CommonOptionsParser):
    """A variant of OptionParser that parses subcommands and their
    arguments.
    """

    def __init__(self, *args, **kwargs):
        """Create a new subcommand-aware option parser. All of the
        options to OptionParser.__init__ are supported in addition
        to subcommands, a sequence of Subcommand objects.
        """
        # A more helpful default usage.
        if "usage" not in kwargs:
            kwargs["usage"] = """
  %prog COMMAND [ARGS...]
  %prog help COMMAND"""
        kwargs["add_help_option"] = False

        # Super constructor.
        super().__init__(*args, **kwargs)

        # Our root parser needs to stop on the first unrecognized argument.
        self.disable_interspersed_args()

        self.subcommands = []

    def add_subcommand(self, *cmds):
        """Adds a Subcommand object to the parser's list of commands."""
        for cmd in cmds:
            cmd.root_parser = self
            self.subcommands.append(cmd)

    # Add the list of subcommands to the help message.
    def format_help(self, formatter=None):
        # Get the original help message, to which we will append.
        out = super().format_help(formatter)
        if formatter is None:
            formatter = self.formatter

        # Subcommands header.
        result = ["\n"]
        result.append(formatter.format_heading("Commands"))
        formatter.indent()

        # Generate the display names (including aliases).
        # Also determine the help position.
        disp_names = []
        help_position = 0
        subcommands = [c for c in self.subcommands if not c.hide]
        subcommands.sort(key=lambda c: c.name)
        for subcommand in subcommands:
            name = subcommand.name
            if subcommand.aliases:
                name += f" ({', '.join(subcommand.aliases)})"
            disp_names.append(name)

            # Set the help position based on the max width.
            proposed_help_position = len(name) + formatter.current_indent + 2
            if proposed_help_position <= formatter.max_help_position:
                help_position = max(help_position, proposed_help_position)

        # Add each subcommand to the output.
        for subcommand, name in zip(subcommands, disp_names):
            # Lifted directly from optparse.py.
            name_width = help_position - formatter.current_indent - 2
            if len(name) > name_width:
                name = f"{' ' * formatter.current_indent}{name}\n"
                indent_first = help_position
            else:
                name = f"{' ' * formatter.current_indent}{name:<{name_width}}\n"
                indent_first = 0
            result.append(name)
            help_width = formatter.width - help_position
            help_lines = textwrap.wrap(subcommand.help, help_width)
            help_line = help_lines[0] if help_lines else ""
            result.append(f"{' ' * indent_first}{help_line}\n")
            result.extend(
                [f"{' ' * help_position}{line}\n" for line in help_lines[1:]]
            )
        formatter.dedent()

        # Concatenate the original help message with the subcommand
        # list.
        return f"{out}{''.join(result)}"

    def _subcommand_for_name(self, name):
        """Return the subcommand in self.subcommands matching the
        given name. The name may either be the name of a subcommand or
        an alias. If no subcommand matches, returns None.
        """
        for subcommand in self.subcommands:
            if name == subcommand.name or name in subcommand.aliases:
                return subcommand
        return None

    def parse_global_options(self, args):
        """Parse options up to the subcommand argument. Returns a tuple
        of the options object and the remaining arguments.
        """
        options, subargs = self.parse_args(args)

        # Force the help command
        if options.help:
            subargs = ["help"]
        elif options.version:
            subargs = ["version"]
        return options, subargs

    def parse_subcommand(self, args):
        """Given the `args` left unused by a `parse_global_options`,
        return the invoked subcommand, the subcommand options, and the
        subcommand arguments.
        """
        # Help is default command
        if not args:
            args = ["help"]

        cmdname = args.pop(0)
        subcommand = self._subcommand_for_name(cmdname)
        if not subcommand:
            raise UserError(f"unknown command '{cmdname}'")

        suboptions, subargs = subcommand.parse_args(args)
        return subcommand, suboptions, subargs


optparse.Option.ALWAYS_TYPED_ACTIONS += ("callback",)


# The main entry point and bootstrapping.


def _setup(
    options: optparse.Values, lib: library.Library | None
) -> tuple[list[Subcommand], library.Library]:
    """Prepare and global state and updates it with command line options.

    Returns a list of subcommands, a list of plugins, and a library instance.
    """
    config = _configure(options)

    plugins.load_plugins()

    # Get the default subcommands.
    from beets.ui.commands import default_commands

    subcommands = list(default_commands)
    subcommands.extend(plugins.commands())

    if lib is None:
        lib = _open_library(config)
        plugins.send("library_opened", lib=lib)

    return subcommands, lib


def setup_logging():
    level = logging.DEBUG if config["verbose"].get(int) else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    console = get_console()
    if not root.handlers:
        handler = SafeRichHandler(
            show_path=False,
            show_level=True,
            omit_repeated_times=False,
            rich_tracebacks=True,
            tracebacks_show_locals=True,
            tracebacks_width=console.width,
            tracebacks_extra_lines=1,
            keywords=["Sending event"],
            markup=True,
            console=console,
        )
        handler.setFormatter(
            logging.Formatter(
                "[b grey42]{name:<20}[/] {message}", datefmt="%T", style="{"
            )
        )
        root.addHandler(handler)

    root.propagate = False  # Don't propagate to root handler.

    install(
        console=console,
        show_locals=True,
        width=console.width,
        code_width=console.width,
        locals_max_length=1,
        locals_hide_sunder=True,
    )


def _configure(options):
    """Amend the global configuration object with command line options."""
    # Add any additional config files specified with --config. This
    # special handling lets specified plugins get loaded before we
    # finish parsing the command line.
    if getattr(options, "config", None) is not None:
        overlay_path = options.config
        del options.config
        config.set_file(overlay_path)
    else:
        overlay_path = None
    config.set_args(options)

    setup_logging()

    if overlay_path:
        log.debug(
            "overlaying configuration: {}", util.displayable_path(overlay_path)
        )

    config_path = config.user_config_path()
    if os.path.isfile(config_path):
        log.debug("user configuration: {}", util.displayable_path(config_path))
    else:
        log.debug(
            "no user configuration found at {}",
            util.displayable_path(config_path),
        )

    log.debug("data directory: {}", util.displayable_path(config.config_dir()))
    return config


def _ensure_db_directory_exists(path):
    if path == b":memory:":  # in memory db
        return
    newpath = os.path.dirname(path)
    if not os.path.isdir(newpath):
        if input_yn(
            f"The database directory {util.displayable_path(newpath)} does not"
            " exist. Create it (Y/n)?"
        ):
            os.makedirs(newpath)


def _open_library(config: confuse.LazyConfig) -> library.Library:
    """Create a new library instance from the configuration."""
    dbpath = util.bytestring_path(config["library"].as_filename())
    _ensure_db_directory_exists(dbpath)
    try:
        lib = library.Library(
            dbpath,
            config["directory"].as_filename(),
            get_path_formats(),
            get_replacements(),
        )
        lib.get_item(0)  # Test database connection.
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as db_error:
        log.debug("{}", traceback.format_exc())
        raise UserError(
            f"database file {util.displayable_path(dbpath)} cannot not be"
            f" opened: {db_error}"
        )
    log.debug(
        "library database: {}\nlibrary directory: {}",
        util.displayable_path(lib.path),
        util.displayable_path(lib.directory),
    )
    return lib


def _raw_main(args: list[str], lib=None) -> None:
    """A helper function for `main` without top-level exception
    handling.
    """
    parser = SubcommandsOptionParser()
    parser.add_format_option(flags=("--format-item",), target=library.Item)
    parser.add_format_option(flags=("--format-album",), target=library.Album)
    parser.add_option(
        "-l", "--library", dest="library", help="library database file to use"
    )
    parser.add_option(
        "-d",
        "--directory",
        dest="directory",
        help="destination music directory",
    )
    parser.add_option(
        "-v",
        "--verbose",
        dest="verbose",
        action="count",
        help="log more details (use twice for even more)",
    )
    parser.add_option(
        "-c", "--config", dest="config", help="path to configuration file"
    )

    def parse_csl_callback(
        option: optparse.Option, _, value: str, parser: SubcommandsOptionParser
    ):
        """Parse a comma-separated list of values."""
        setattr(
            parser.values,
            option.dest,  # type: ignore[arg-type]
            list(filter(None, value.split(","))),
        )

    parser.add_option(
        "-p",
        "--plugins",
        dest="plugins",
        action="callback",
        callback=parse_csl_callback,
        help="a comma-separated list of plugins to load",
    )
    parser.add_option(
        "-P",
        "--disable-plugins",
        dest="disabled_plugins",
        action="callback",
        callback=parse_csl_callback,
        help="a comma-separated list of plugins to disable",
    )
    parser.add_option(
        "-h",
        "--help",
        dest="help",
        action="store_true",
        help="show this help message and exit",
    )
    parser.add_option(
        "--version",
        dest="version",
        action="store_true",
        help=optparse.SUPPRESS_HELP,
    )

    options, subargs = parser.parse_global_options(args)

    # Special case for the `config --edit` command: bypass _setup so
    # that an invalid configuration does not prevent the editor from
    # starting.
    if (
        subargs
        and subargs[0] == "config"
        and ("-e" in subargs or "--edit" in subargs)
    ):
        from beets.ui.commands import config_edit

        return config_edit()

    test_lib = bool(lib)
    subcommands, lib = _setup(options, lib)
    parser.add_subcommand(*subcommands)

    subcommand, suboptions, subargs = parser.parse_subcommand(subargs)
    subcommand.func(lib, suboptions, subargs)

    plugins.send("cli_exit", lib=lib)
    if not test_lib:
        # Clean up the library unless it came from the test harness.
        lib._close()


def main(args=None):
    """Run the main command-line interface for beets. Includes top-level
    exception handlers that print friendly error messages.
    """
    if "AppData\\Local\\Microsoft\\WindowsApps" in sys.exec_prefix:
        log.error(
            "error: beets is unable to use the Microsoft Store version of "
            "Python. Please install Python from https://python.org.\n"
            "error: More details can be found here "
            "https://beets.readthedocs.io/en/stable/guides/main.html"
        )
        sys.exit(1)
    try:
        _raw_main(args)
    except UserError as exc:
        message = exc.args[0] if exc.args else None
        if "No matching" in message:
            log.error("error: {}", message)
        else:
            get_console().print_exception(extra_lines=2, show_locals=True)
        sys.exit(1)
    except util.HumanReadableError as exc:
        exc.log(log)
        sys.exit(1)
    except library.FileOperationError as exc:
        # These errors have reasonable human-readable descriptions, but
        # we still want to log their tracebacks for debugging.
        log.debug("{}", traceback.format_exc())
        log.error("{}", exc)
        sys.exit(1)
    except confuse.ConfigError as exc:
        log.error("configuration error: {}", exc)
        sys.exit(1)
    except db_query.InvalidQueryError as exc:
        log.error("invalid query: {}", exc)
        sys.exit(1)
    except OSError as exc:
        if exc.errno == errno.EPIPE:
            # "Broken pipe". End silently.
            sys.stderr.close()
        else:
            raise
    except KeyboardInterrupt:
        # Silently ignore ^C except in verbose mode.
        log.debug("{}", traceback.format_exc())
    except db.DBAccessError as exc:
        log.error(
            "database access error: {}\n"
            "the library file might have a permissions problem",
            exc,
        )
        sys.exit(1)
