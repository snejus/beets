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

"""This module provides the default commands for beets' command-line
interface.
"""

from __future__ import annotations

import os
import re
import textwrap
from collections import Counter
from itertools import chain
from platform import python_version
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import beets
from beets import config, importer, library, logging, plugins, ui, util
from beets.autotag.hooks import Match
from beets.autotag.match import Recommendation
from beets.ui import input_, print_
from beets.util import (
    MoveOperation,
    ancestry,
    displayable_path,
    normpath,
    syspath,
)
from beets.util.units import human_bytes, human_seconds

from ..exceptions import UserError
from . import _store_dict
from .display import AlbumView, SingletonView, View

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.autotag.hooks import AnyMatch
    from beets.importer import Action, ImportTask


# Global logger.
log = logging.getLogger(__name__)

# The list of default subcommands. This is populated with Subcommand
# objects that can be fed to a SubcommandsOptionParser.
default_commands = []


# Utilities.


def _do_query(
    lib: library.Library, query: str, album: bool, also_items: bool = True
) -> tuple[list[library.Item], list[library.Album]]:
    """For commands that operate on matched items, performs a query
    and returns a list of matching items and a list of matching
    albums. (The latter is only nonempty when album is True.) Raises
    a UserError if no items match. also_items controls whether, when
    fetching albums, the associated items should be fetched also.
    """
    if album:
        albums = list(lib.albums(query))
        items: list[library.Item] = []
        if also_items:
            for al in albums:
                items += al.items()

    else:
        albums = []
        items = list(lib.items(query))

    if album and not albums:
        raise UserError("No matching albums found.")
    elif not album and not items:
        raise UserError("No matching items found.")

    return items, albums


def _paths_from_logfile(path):
    """Parse the logfile and yield skipped paths to pass to the `import`
    command.
    """
    with open(path, encoding="utf-8") as fp:
        for i, line in enumerate(fp, start=1):
            verb, sep, paths = line.rstrip("\n").partition(" ")
            if not sep:
                raise ValueError(f"line {i} is invalid")

            # Ignore informational lines that don't need to be re-imported.
            if verb in {"import", "duplicate-keep", "duplicate-replace"}:
                continue

            if verb not in {"asis", "skip", "duplicate-skip"}:
                raise ValueError(f"line {i} contains unknown verb {verb}")

            yield os.path.commonpath(paths.split("; "))


def _parse_logfiles(logfiles):
    """Parse all `logfiles` and yield paths from it."""
    for logfile in logfiles:
        try:
            yield from _paths_from_logfile(syspath(normpath(logfile)))
        except ValueError as err:
            raise UserError(
                f"malformed logfile {util.displayable_path(logfile)}: {err}"
            ) from err
        except OSError as err:
            raise UserError(
                f"unreadable logfile {util.displayable_path(logfile)}: {err}"
            ) from err


# fields: Shows a list of available fields for queries and format strings.


def _print_keys(query):
    """Given a SQLite query result, print the `key` field of each
    returned row, with indentation of 2 spaces.
    """
    for row in query:
        print_(f"  {row['key']}")


def fields_func(lib, opts, args):
    def _print_rows(names):
        print_(textwrap.indent("\n".join(sorted(names))))

    print_("Item fields:")
    _print_rows(library.Item.all_keys())

    print_("Album fields:")
    _print_rows(library.Album.all_keys())

    with lib.transaction() as tx:
        # The SQL uses the DISTINCT to get unique values from the query
        unique_fields = "SELECT DISTINCT key FROM ({})"

        print_("Item flexible attributes:")
        _print_keys(tx.query(unique_fields.format(library.Item._flex_table)))

        print_("Album flexible attributes:")
        _print_keys(tx.query(unique_fields.format(library.Album._flex_table)))


fields_cmd = ui.Subcommand(
    "fields", help="show fields available for queries and format strings"
)
fields_cmd.func = fields_func
default_commands.append(fields_cmd)


# help: Print help text for commands


class HelpCommand(ui.Subcommand):
    def __init__(self):
        super().__init__(
            "help",
            aliases=("?",),
            help="give detailed help on a specific sub-command",
        )

    def func(self, lib, opts, args):
        if args:
            cmdname = args[0]
            helpcommand = self.root_parser._subcommand_for_name(cmdname)
            if not helpcommand:
                raise UserError(f"unknown command '{cmdname}'")
            helpcommand.print_help()
        else:
            self.root_parser.print_help()


default_commands.append(HelpCommand())


# Importer utilities and support.


def summarize_items(items: list[library.Item], singleton: bool) -> str:
    """Produces a brief summary line describing a set of items. Used for
    manually resolving duplicates during import.

    `items` is a list of `Item` objects. `singleton` indicates whether
    this is an album or single-item import (if the latter, them `items`
    should only have one element).
    """
    summary_parts = []
    if not singleton:
        summary_parts.append(f"{len(items)} items")

    format_counts = Counter(i.format for i in items)

    if len(format_counts) == 1:
        # A single format.
        summary_parts.append(items[0].format)
    else:
        summary_parts.extend(f"{f} {c}" for f, c in format_counts.items())

    average_bitrate = sum(item.bitrate for item in items) / len(items)
    summary_parts.append(f"{average_bitrate / 1000:0f}kbps")

    if (item := items[0]).format == "FLAC":
        summary_parts.append(
            f"{item.samplerate / 1000:.1f}kHz/{item.bitdepth} bit"
        )

    duration = sum(item.length for item in items)
    summary_parts.append(f"{duration // 60:n}:{duration % 60:.0f}")
    total_filesize = sum(item.filesize for item in items)
    summary_parts.append(human_bytes(total_filesize))

    return ", ".join(summary_parts)


def _summary_judgment(rec):
    """Determines whether a decision should be made without even asking
    the user. This occurs in quiet mode and when an action is chosen for
    NONE recommendations. Return None if the user should be queried.
    Otherwise, returns an action. May also print to the console if a
    summary judgment is made.
    """

    if config["import"]["quiet"]:
        if rec == Recommendation.strong:
            return importer.Action.APPLY
        else:
            action = config["import"]["quiet_fallback"].as_choice(
                {
                    "skip": importer.Action.SKIP,
                    "asis": importer.Action.ASIS,
                }
            )
    elif config["import"]["timid"]:
        return None
    elif rec == Recommendation.none:
        action = config["import"]["none_rec_action"].as_choice(
            {
                "skip": importer.Action.SKIP,
                "asis": importer.Action.ASIS,
                "ask": None,
            }
        )
    else:
        return None

    if action == importer.Action.SKIP:
        print_("Skipping.")
    elif action == importer.Action.ASIS:
        print_("Importing as-is.")
    return action


class PromptChoice(NamedTuple):
    short: str
    long: str
    callback: Any


def choose_candidate(
    view: View[AnyMatch],
    candidates: Sequence[AnyMatch],
    rec: Recommendation,
    choices: list[PromptChoice],
) -> PromptChoice | AnyMatch:
    """Given a sorted list of candidates, ask the user for a selection
    of which candidate to use. Applies to both full albums and
    singletons  (tracks).

    `choices` is a list of `PromptChoice`s to be used in each prompt.

    Returns one of the following:
    * the result of the choice, which may be SKIP or ASIS
    * a candidate (an AlbumMatch/TrackMatch object)
    * a chosen `PromptChoice` from `choices`
    """
    # Build helper variables for the prompt choices.
    choice_opts = tuple(c.long for c in choices)
    choice_actions = {c.short: c for c in choices}

    # Zero candidates.
    if not candidates:
        view.print_not_found()
        return choice_actions[ui.input_options(choice_opts)]

    # Is the change good enough?
    selected_idx = 0
    show_candidates = rec == Recommendation.none

    while True:
        # Display and choose from candidates.
        highlight_default_choice = rec > Recommendation.low

        if show_candidates:
            # Display list of candidates.
            view.print_candidates()

            # Ask the user for a choice.
            sel = ui.input_options(choice_opts, numrange=(1, len(candidates)))
            if sel == "m":
                pass
            elif sel in choice_actions:
                return choice_actions[sel]
            else:  # Numerical selection.
                selected_idx = int(sel) - 1
                if selected_idx != 0:
                    # When choosing anything but the first match,
                    # disable the default action.
                    highlight_default_choice = False
        show_candidates = True

        # Show what we're about to do.
        match = view.show_match(selected_idx)

        # Exact match => tag automatically if we're not in timid mode.
        if rec == Recommendation.strong and not config["import"]["timid"]:
            return match

        # Ask for confirmation.
        default = config["import"]["default_action"].as_choice(
            {
                "apply": "a",
                "skip": "s",
                "asis": "u",
                "none": None,
            }
        )
        if default is None:
            highlight_default_choice = False
        # Bell ring when user interaction is needed.
        if config["import"]["bell"]:
            ui.print_("\a", end="")
        sel = ui.input_options(
            ("Apply", "More candidates") + choice_opts,
            highlight_default=highlight_default_choice,
            default=default,
        )
        if sel == "a":
            return match
        elif sel in choice_actions:
            return choice_actions[sel]


def manual_search(
    session: importer.ImportSession, task: importer.ImportTask[AnyMatch]
) -> None:
    """Update task with candidates using manual search criteria.

    Input either an artist and album (for full albums) or artist and
    track name (for singletons) for manual search.
    """
    task.lookup_candidates(
        search_artist=input_("Artist:").strip(),
        search_name=input_("Album:" if task.is_album else "Track:").strip(),
    )


def manual_id(
    session: importer.ImportSession, task: importer.ImportTask[AnyMatch]
) -> None:
    """Update task with candidates using a manually-entered ID.

    Input an ID, either for an album ("release") or a track ("recording").
    """
    _type = "release" if task.is_album else "recording"
    task.lookup_candidates(
        search_ids=input_(f"Enter {_type} ID:").strip().split()
    )


def abort_action(session, task):
    """A prompt choice callback that aborts the importer."""
    raise importer.ImportAbortError()


class TerminalImportSession(importer.ImportSession):
    """An import session that runs in a terminal."""

    def choose_match(self, task: ImportTask[Any]) -> Match | Action:
        # Let plugins display info or prompt the user before we go through the
        # process of selecting candidate.
        view: View[Any]
        if isinstance(task, importer.AlbumImportTask):
            view = AlbumView(task)
        else:
            view = SingletonView(task)

        results = plugins.send(
            "import_task_before_choice", session=self, task=task
        )
        actions = [action for action in results if action]

        if len(actions) == 1:
            return actions[0]
        elif len(actions) > 1:
            raise plugins.PluginConflictError(
                "Only one handler for `import_task_before_choice` may return "
                "an action."
            )

        # Take immediate action if appropriate.
        action = _summary_judgment(task.rec)
        if action == importer.Action.APPLY:
            return view.show_match(0)
        elif action is not None:
            return action

        # Loop until we have a choice.
        while True:
            # Ask for a choice from the user. The result of
            # `choose_candidate` may be an `importer.Action`, an
            # `AlbumMatch` object for a specific selection, or a
            # `PromptChoice`.
            choices = self._get_choices(task)
            choice = choose_candidate(
                view, task.candidates, task.rec, choices=choices
            )
            if isinstance(choice, Match):
                # We have a candidate! Finish tagging. Here, choice is an
                # AlbumMatch object.
                return choice

            # Plugin-provided choices. We invoke the associated callback
            # function.
            if post_choice := choice.callback(self, task):
                return post_choice

    def decide_duplicates(
        self, task: importer.ImportTask[AnyMatch], duplicates: list[ui.AnyModel]
    ) -> str:
        """Decide what to do when a new album or item seems similar to one
        that's already in the library.
        """
        log.warning(
            "This {} is already in the library!",
            ("album" if task.is_album else "item"),
        )

        if config["import"]["quiet"]:
            # In quiet mode, don't prompt -- just skip.
            log.info("Skipping.")
            return "s"
        # Print some detail about the existing and new items so the
        # user can make an informed decision.
        for duplicate in duplicates:
            dupes = list(duplicate.items()) if task.is_album else [duplicate]
            print_("Old: " + summarize_items(dupes, not task.is_album))

            if config["import"]["duplicate_verbose_prompt"]:
                for dup in dupes:
                    print(f"  {dup}")

        items = task.imported_items()
        print_("New: " + summarize_items(items, not task.is_album))

        if config["import"]["duplicate_verbose_prompt"]:
            for item in task.imported_items():
                print(f"  {item}")

        return ui.input_options(importer.DuplicateAction.options())

    def should_resume(self, path):
        return ui.input_yn(
            f"Import of the directory:\n{displayable_path(path)}\n"
            "was interrupted. Resume?"
        )

    def _get_choices(self, task):
        """Get the list of prompt choices that should be presented to the
        user. This consists of both built-in choices and ones provided by
        plugins.

        The `before_choose_candidate` event is sent to the plugins, with
        session and task as its parameters. Plugins are responsible for
        checking the right conditions and returning a list of `PromptChoice`s,
        which is flattened and checked for conflicts.

        If two or more choices have the same short letter, a warning is
        emitted and all but one choices are discarded, giving preference
        to the default importer choices.

        Returns a list of `PromptChoice`s.
        """
        # Standard, built-in choices.
        choices = [
            PromptChoice("s", "Skip", lambda s, t: importer.Action.SKIP),
            PromptChoice("u", "Use as-is", lambda s, t: importer.Action.ASIS),
        ]
        if task.is_album:
            choices += [
                PromptChoice(
                    "t", "as Tracks", lambda s, t: importer.Action.TRACKS
                ),
                PromptChoice(
                    "g", "Group albums", lambda s, t: importer.Action.ALBUMS
                ),
            ]
        choices += [
            PromptChoice("e", "Enter search", manual_search),
            PromptChoice("i", "enter Id", manual_id),
            PromptChoice("b", "aBort", abort_action),
        ]

        # Send the before_choose_candidate event and flatten list.
        extra_choices = list(
            chain(
                *plugins.send(
                    "before_choose_candidate", session=self, task=task
                )
            )
        )

        # Add a "dummy" choice for the other baked-in option, for
        # duplicate checking.
        all_choices = (
            [
                PromptChoice("a", "Apply", None),
            ]
            + choices
            + extra_choices
        )

        # Check for conflicts.
        short_letters = [c.short for c in all_choices]
        if len(short_letters) != len(set(short_letters)):
            # Duplicate short letter has been found.
            duplicates = [
                i for i, count in Counter(short_letters).items() if count > 1
            ]
            for short in duplicates:
                # Keep the first of the choices, removing the rest.
                dup_choices = [c for c in all_choices if c.short == short]
                for c in dup_choices[1:]:
                    log.warning(
                        "Prompt choice '{0.long}' removed due to conflict "
                        "with '{1[0].long}' (short letter: '{0.short}')",
                        c,
                        dup_choices,
                    )
                    extra_choices.remove(c)

        return choices + extra_choices


# The import command.


def import_files(
    lib: library.Library, paths: list[bytes] | None, **kwargs
) -> None:
    """Import the files in the given list of paths or matching the query."""
    # Check parameter consistency.
    if config["import"]["quiet"] and config["import"]["timid"]:
        raise UserError("can't be both quiet and timid")

    # Never ask for input in quiet mode.
    if config["import"]["resume"].get() == "ask" and config["import"]["quiet"]:
        config["import"]["resume"] = False

    session = TerminalImportSession.make(lib, paths=paths, **kwargs)
    session.run()

    # Emit event.
    plugins.send("import", lib=lib, paths=paths)


def import_func(lib, opts, args: list[str]):
    config["import"].set_args(opts)

    # Special case: --copy flag suppresses import_move (which would
    # otherwise take precedence).
    if opts.copy:
        config["import"]["move"] = False

    if opts.library:
        query = args
        byte_paths = []
    else:
        query = None
        paths = args

        # The paths from the logfiles go into a separate list to allow handling
        # errors differently from user-specified paths.
        paths_from_logfiles = list(_parse_logfiles(opts.from_logfiles or []))

        if not paths and not paths_from_logfiles:
            raise UserError("no path specified")

        byte_paths = [os.fsencode(p) for p in paths]
        paths_from_logfiles = [os.fsencode(p) for p in paths_from_logfiles]

        # Check the user-specified directories.
        for invalid_path in (p for p in byte_paths if not os.path.exists(p)):
            raise UserError(
                f"No such file or directory: {displayable_path(invalid_path)}"
            )

        # Check the directories from the logfiles, but don't throw an error in
        # case those paths don't exist. Maybe some of those paths have already
        # been imported and moved separately, so logging a warning should
        # suffice.
        for path in paths_from_logfiles:
            if not os.path.exists(path):
                log.warning(
                    "No such file or directory: {}", displayable_path(path)
                )
                continue

            byte_paths.append(path)

        # If all paths were read from a logfile, and none of them exist, throw
        # an error
        if not paths:
            raise UserError("none of the paths are importable")

    import_files(lib, paths=byte_paths, query=query)


import_cmd = ui.Subcommand(
    "import", help="import new music", aliases=("imp", "im")
)
import_cmd.parser.add_option(
    "-c",
    "--copy",
    action="store_true",
    default=None,
    help="copy tracks into library directory (default)",
)
import_cmd.parser.add_option(
    "-C",
    "--nocopy",
    action="store_false",
    dest="copy",
    help="don't copy tracks (opposite of -c)",
)
import_cmd.parser.add_option(
    "-m",
    "--move",
    action="store_true",
    dest="move",
    help="move tracks into the library (overrides -c)",
)
import_cmd.parser.add_option(
    "-w",
    "--write",
    action="store_true",
    default=None,
    help="write new metadata to files' tags (default)",
)
import_cmd.parser.add_option(
    "-W",
    "--nowrite",
    action="store_false",
    dest="write",
    help="don't write metadata (opposite of -w)",
)
import_cmd.parser.add_option(
    "-a",
    "--autotag",
    action="store_true",
    dest="autotag",
    help="infer tags for imported files (default)",
)
import_cmd.parser.add_option(
    "-A",
    "--noautotag",
    action="store_false",
    dest="autotag",
    help="don't infer tags for imported files (opposite of -a)",
)
import_cmd.parser.add_option(
    "-p",
    "--resume",
    action="store_true",
    default=None,
    help="resume importing if interrupted",
)
import_cmd.parser.add_option(
    "-P",
    "--noresume",
    action="store_false",
    dest="resume",
    help="do not try to resume importing",
)
import_cmd.parser.add_option(
    "-q",
    "--quiet",
    action="store_true",
    dest="quiet",
    help="never prompt for input: skip albums instead",
)
import_cmd.parser.add_option(
    "--quiet-fallback",
    type="string",
    dest="quiet_fallback",
    help="decision in quiet mode when no strong match: skip or asis",
)
import_cmd.parser.add_option(
    "-l",
    "--log",
    dest="log",
    help="file to log untaggable albums for later review",
)
import_cmd.parser.add_option(
    "-s",
    "--singletons",
    action="store_true",
    help="import individual tracks instead of full albums",
)
import_cmd.parser.add_option(
    "-t",
    "--timid",
    dest="timid",
    action="store_true",
    help="always confirm all actions",
)
import_cmd.parser.add_option(
    "-L",
    "--library",
    dest="library",
    action="store_true",
    help="retag items matching a query",
)
import_cmd.parser.add_option(
    "-i",
    "--incremental",
    dest="incremental",
    action="store_true",
    help="skip already-imported directories",
)
import_cmd.parser.add_option(
    "-I",
    "--noincremental",
    dest="incremental",
    action="store_false",
    help="do not skip already-imported directories",
)
import_cmd.parser.add_option(
    "-R",
    "--incremental-skip-later",
    action="store_true",
    dest="incremental_skip_later",
    help="do not record skipped files during incremental import",
)
import_cmd.parser.add_option(
    "-r",
    "--noincremental-skip-later",
    action="store_false",
    dest="incremental_skip_later",
    help="record skipped files during incremental import",
)
import_cmd.parser.add_option(
    "--from-scratch",
    dest="from_scratch",
    action="store_true",
    help="erase existing metadata before applying new metadata",
)
import_cmd.parser.add_option(
    "--flat",
    dest="flat",
    action="store_true",
    help="import an entire tree as a single album",
)
import_cmd.parser.add_option(
    "-g",
    "--group-albums",
    dest="group_albums",
    action="store_true",
    help="group tracks in a folder into separate albums",
)
import_cmd.parser.add_option(
    "--pretend",
    dest="pretend",
    action="store_true",
    help="just print the files to import",
)
import_cmd.parser.add_option(
    "-S",
    "--search-id",
    dest="search_ids",
    action="append",
    metavar="ID",
    help="restrict matching to a specific metadata backend ID",
)
import_cmd.parser.add_option(
    "--from-logfile",
    dest="from_logfiles",
    action="append",
    metavar="PATH",
    help="read skipped paths from an existing logfile",
)
import_cmd.parser.add_option(
    "--set",
    dest="set_fields",
    action="callback",
    callback=_store_dict,
    metavar="FIELD=VALUE",
    help="set the given fields to the supplied values",
)
import_cmd.func = import_func
default_commands.append(import_cmd)


# list: Query and show library contents.


def list_items(lib, query, album, fmt=""):
    """Print out items in lib matching query. If album, then search for
    albums instead of single items.
    """
    if album:
        for album in lib.albums(query):
            ui.print_(format(album, fmt))
    else:
        for item in lib.items(query):
            ui.print_(format(item, fmt))


def list_func(lib, opts, args):
    list_items(lib, args, opts.album)


list_cmd = ui.Subcommand("list", help="query the library", aliases=("ls",))
list_cmd.parser.usage += "\nExample: %prog -f '$album: $title' artist:beatles"
list_cmd.parser.add_all_common_options()
list_cmd.func = list_func
default_commands.append(list_cmd)


# update: Update library contents according to on-disk tags.


def update_items(lib, query, album, move, pretend, fields, exclude_fields=None):
    """For all the items matched by the query, update the library to
    reflect the item's embedded tags.
    :param fields: The fields to be stored. If not specified, all fields will
    be.
    :param exclude_fields: The fields to not be stored. If not specified, all
    fields will be.
    """
    with lib.transaction():
        items, _ = _do_query(lib, query, album)
        if move and fields is not None and "path" not in fields:
            # Special case: if an item needs to be moved, the path field has to
            # updated; otherwise the new path will not be reflected in the
            # database.
            fields.append("path")
        if fields is None:
            # no fields were provided, update all media fields
            item_fields = fields or library.Item._media_fields
            if move and "path" not in item_fields:
                # move is enabled, add 'path' to the list of fields to update
                item_fields.add("path")
        else:
            # fields was provided, just update those
            item_fields = fields
        # get all the album fields to update
        album_fields = fields or library.Album._fields.keys()
        if exclude_fields:
            # remove any excluded fields from the item and album sets
            item_fields = [f for f in item_fields if f not in exclude_fields]
            album_fields = [f for f in album_fields if f not in exclude_fields]

        # Walk through the items and pick up their changes.
        affected_albums = set()
        for item in items:
            # Item deleted?
            if not item.path or not os.path.exists(syspath(item.path)):
                ui.print_(format(item))
                ui.print_(ui.colorize("text_error", "  deleted"))
                if not pretend:
                    item.remove(True)
                affected_albums.add(item.album_id)
                continue

            # Did the item change since last checked?
            if item.current_mtime() <= item.mtime:
                log.debug(
                    "skipping {0.filepath} because mtime is up to date ({0.mtime})",
                    item,
                )
                continue

            # Read new data.
            try:
                item.read()
            except library.ReadError as exc:
                log.error("error reading {.filepath}: {}", item, exc)
                continue

            # Special-case album artist when it matches track artist. (Hacky
            # but necessary for preserving album-level metadata for non-
            # autotagged imports.)
            old_item = lib.get_item(item.id)
            if not item.albumartist:
                if old_item.albumartist == old_item.artist == item.artist:
                    item.albumartist = old_item.albumartist
                    item._dirty.discard("albumartist")

            # Check for and display changes.
            changed = ui.show_model_changes(
                item, old=old_item, fields=fields or library.Item._media_fields
            )

            # Save changes.
            if not pretend:
                if changed:
                    # Move the item if it's in the library.
                    if move and lib.directory in ancestry(item.path):
                        item.move(store=False)

                    item.store(fields=item_fields)
                    affected_albums.add(item.album_id)
                else:
                    # The file's mtime was different, but there were no
                    # changes to the metadata. Store the new mtime,
                    # which is set in the call to read(), so we don't
                    # check this again in the future.
                    item.store(fields=item_fields)

        # Skip album changes while pretending.
        if pretend:
            return

        # Modify affected albums to reflect changes in their items.
        for album_id in affected_albums:
            if album_id is None:  # Singletons.
                continue
            album = lib.get_album(album_id)
            if not album:  # Empty albums have already been removed.
                log.debug("emptied album {}", album_id)
                continue
            first_item = album.items().get()

            # Update album structure to reflect an item in it.
            for key in library.Album.item_keys:
                album[key] = first_item[key]
            album.store(fields=album_fields)

            # Move album art (and any inconsistent items).
            if move and lib.directory in ancestry(first_item.path):
                log.debug("moving album {}", album_id)

                # Manually moving and storing the album.
                items = list(album.items())
                for item in items:
                    item.move(store=False, with_album=False)
                    item.store(fields=item_fields)
                album.move(store=False)
                album.store(fields=album_fields)


def update_func(lib, opts, args):
    # Verify that the library folder exists to prevent accidental wipes.
    if not os.path.isdir(syspath(lib.directory)):
        ui.print_("Library path is unavailable or does not exist.")
        ui.print_(lib.directory)
        if not ui.input_yn(
            "Are you sure you want to continue?", highlight_default=False
        ):
            return
    update_items(
        lib,
        args,
        opts.album,
        ui.should_move(opts.move),
        opts.pretend,
        opts.fields,
        opts.exclude_fields,
    )


update_cmd = ui.Subcommand(
    "update",
    help="update the library",
    aliases=(
        "upd",
        "up",
    ),
)
update_cmd.parser.add_album_option()
update_cmd.parser.add_format_option()
update_cmd.parser.add_option(
    "-m",
    "--move",
    action="store_true",
    dest="move",
    help="move files in the library directory",
)
update_cmd.parser.add_option(
    "-M",
    "--nomove",
    action="store_false",
    dest="move",
    help="don't move files in library",
)
update_cmd.parser.add_option(
    "-p",
    "--pretend",
    action="store_true",
    help="show all changes but do nothing",
)
update_cmd.parser.add_option(
    "-F",
    "--field",
    default=None,
    action="append",
    dest="fields",
    help="list of fields to update",
)
update_cmd.parser.add_option(
    "-e",
    "--exclude-field",
    default=None,
    action="append",
    dest="exclude_fields",
    help="list of fields to exclude from updates",
)
update_cmd.func = update_func
default_commands.append(update_cmd)


# remove: Remove items from library, delete files.


def remove_items(lib, query, album, delete, force):
    """Remove items matching query from lib. If album, then match and
    remove whole albums. If delete, also remove files from disk.
    """
    # Get the matching items.
    items, albums = _do_query(lib, query, album)
    objs = albums if album else items

    # Confirm file removal if not forcing removal.
    if not force:
        # Prepare confirmation with user.
        album_str = (
            f" in {len(albums)} album{'s' if len(albums) > 1 else ''}"
            if album
            else ""
        )

        if delete:
            fmt = "$path - $title"
            prompt = "Really DELETE"
            prompt_all = (
                "Really DELETE"
                f" {len(items)} file{'s' if len(items) > 1 else ''}{album_str}"
            )
        else:
            fmt = ""
            prompt = "Really remove from the library?"
            prompt_all = (
                "Really remove"
                f" {len(items)} item{'s' if len(items) > 1 else ''}{album_str}"
                " from the library?"
            )

        # Helpers for printing affected items
        def fmt_track(t):
            ui.print_(format(t, fmt))

        def fmt_album(a):
            ui.print_()
            for i in a.items():
                fmt_track(i)

        fmt_obj = fmt_album if album else fmt_track

        # Show all the items.
        for o in objs:
            fmt_obj(o)

        # Confirm with user.
        objs = ui.input_select_objects(
            prompt, objs, fmt_obj, prompt_all=prompt_all
        )

    if not objs:
        return

    # Remove (and possibly delete) items.
    with lib.transaction():
        for obj in objs:
            obj.remove(delete)


def remove_func(lib, opts, args):
    remove_items(lib, args, opts.album, opts.delete, opts.force)


remove_cmd = ui.Subcommand(
    "remove", help="remove matching items from the library", aliases=("rm",)
)
remove_cmd.parser.add_option(
    "-d", "--delete", action="store_true", help="also remove files from disk"
)
remove_cmd.parser.add_option(
    "-f", "--force", action="store_true", help="do not ask when removing items"
)
remove_cmd.parser.add_album_option()
remove_cmd.func = remove_func
default_commands.append(remove_cmd)


# stats: Show library/query statistics.


def show_stats(lib, query, exact):
    """Shows some statistics about the matched items."""
    items = lib.items(query)

    total_size = 0
    total_time = 0.0
    total_items = 0
    artists = set()
    albums = set()
    album_artists = set()

    for item in items:
        if exact:
            try:
                total_size += os.path.getsize(syspath(item.path))
            except OSError as exc:
                log.info("could not get size of {.path}: {}", item, exc)
        else:
            total_size += int(item.length * item.bitrate / 8)
        total_time += item.length
        total_items += 1
        artists.add(item.artist)
        album_artists.add(item.albumartist)
        if item.album_id:
            albums.add(item.album_id)

    size_str = human_bytes(total_size)
    if exact:
        size_str += f" ({total_size} bytes)"

    print_(f"""Tracks: {total_items}
Total time: {human_seconds(total_time)}
{f" ({total_time:.2f} seconds)" if exact else ""}
{"Total size" if exact else "Approximate total size"}: {size_str}
Artists: {len(artists)}
Albums: {len(albums)}
Album artists: {len(album_artists)}""")


def stats_func(lib, opts, args):
    show_stats(lib, args, opts.exact)


stats_cmd = ui.Subcommand(
    "stats", help="show statistics about the library or a query"
)
stats_cmd.parser.add_option(
    "-e", "--exact", action="store_true", help="exact size and time"
)
stats_cmd.func = stats_func
default_commands.append(stats_cmd)


# version: Show current beets version.


def show_version(lib, opts, args):
    print_(f"beets version {beets.__version__}")
    print_(f"Python version {python_version()}")
    # Show plugins.
    names = sorted(p.name for p in plugins.find_plugins())
    if names:
        print_("plugins:", ", ".join(names))
    else:
        print_("no plugins loaded")


version_cmd = ui.Subcommand("version", help="output version information")
version_cmd.func = show_version
default_commands.append(version_cmd)


# modify: Declaratively change metadata.


def modify_items(lib, mods, dels, query, write, move, album, confirm, inherit):
    """Modifies matching items according to user-specified assignments and
    deletions.

    `mods` is a dictionary of field and value pairse indicating
    assignments. `dels` is a list of fields to be deleted.
    """
    # Parse key=value specifications into a dictionary.
    model_cls = library.Album if album else library.Item

    # Get the items to modify.
    items, albums = _do_query(lib, query, album, False)
    objs = albums if album else items

    # Apply changes *temporarily*, preview them, and collect modified
    # objects.
    print_(f"Modifying {len(objs)} {'album' if album else 'item'}s.")
    changed = []
    for obj in objs:
        obj_mods = {
            key: model_cls._parse(key, obj.evaluate_fmt(fmt))
            for key, fmt in mods.items()
        }
        if print_and_modify(obj, obj_mods, dels) and obj not in changed:
            changed.append(obj)

    # Still something to do?
    if not changed:
        print_("No changes to make.")
        return

    # Confirm action.
    if confirm:
        if write and move:
            extra = ", move and write tags"
        elif write:
            extra = " and write tags"
        elif move:
            extra = " and move"
        else:
            extra = ""

        changed = ui.input_select_objects(
            f"Really modify{extra}",
            changed,
            lambda o: print_and_modify(o, mods, dels),
        )

    # Apply changes to database and files
    with lib.transaction():
        for obj in changed:
            obj.try_sync(write, move, inherit)


def print_and_modify(obj, mods, dels):
    """Print the modifications to an item and return a bool indicating
    whether any changes were made.

    `mods` is a dictionary of fields and values to update on the object;
    `dels` is a sequence of fields to delete.
    """
    obj.update(mods)
    for field in dels:
        try:
            del obj[field]
        except KeyError:
            pass
    return ui.show_model_changes(obj)


def modify_parse_args(args):
    """Split the arguments for the modify subcommand into query parts,
    assignments (field=value), and deletions (field!).  Returns the result as
    a three-tuple in that order.
    """
    mods = {}
    dels = []
    query = []
    for arg in args:
        if arg.endswith("!") and "=" not in arg and ":" not in arg:
            dels.append(arg[:-1])  # Strip trailing !.
        elif "=" in arg and ":" not in arg.split("=", 1)[0]:
            key, val = arg.split("=", 1)
            mods[key] = val
        else:
            query.append(arg)
    return query, mods, dels


def modify_func(lib, opts, args):
    query, mods, dels = modify_parse_args(args)
    if not mods and not dels:
        raise UserError("no modifications specified")
    modify_items(
        lib,
        mods,
        dels,
        query,
        ui.should_write(opts.write),
        ui.should_move(opts.move),
        opts.album,
        not opts.yes,
        opts.inherit,
    )


modify_cmd = ui.Subcommand(
    "modify", help="change metadata fields", aliases=("mod",)
)
modify_cmd.parser.add_option(
    "-m",
    "--move",
    action="store_true",
    dest="move",
    help="move files in the library directory",
)
modify_cmd.parser.add_option(
    "-M",
    "--nomove",
    action="store_false",
    dest="move",
    help="don't move files in library",
)
modify_cmd.parser.add_option(
    "-w",
    "--write",
    action="store_true",
    default=None,
    help="write new metadata to files' tags (default)",
)
modify_cmd.parser.add_option(
    "-W",
    "--nowrite",
    action="store_false",
    dest="write",
    help="don't write metadata (opposite of -w)",
)
modify_cmd.parser.add_album_option()
modify_cmd.parser.add_format_option(target="item")
modify_cmd.parser.add_option(
    "-y", "--yes", action="store_true", help="skip confirmation"
)
modify_cmd.parser.add_option(
    "-I",
    "--noinherit",
    action="store_false",
    dest="inherit",
    default=True,
    help="when modifying albums, don't also change item data",
)
modify_cmd.func = modify_func
default_commands.append(modify_cmd)


# move: Move/copy files to the library or a new base directory.


def move_items(
    objs: list[library.LibModel],
    dest_path: util.PathLike,
    operation: MoveOperation,
    pretend: bool,
    store: bool,
    entity: Literal["item", "album"],
    confirm: bool,
):
    """Moves or copies items to a new base directory, given by dest. If
    dest is None, then the library's base directory is used, making the
    command "consolidate" files.
    """
    dest = os.fsencode(dest_path) if dest_path else dest_path
    num_objs = len(objs)

    # Filter out files that don't need to be moved.
    objs = [o for o in objs if o._needs_moving(dest_path)]
    num_unmoved = num_objs - len(objs)
    # Report unmoved files that match the query.
    unmoved_msg = ""
    if num_unmoved > 0:
        unmoved_msg = f" ({num_unmoved} already in place)"

    action = operation.name.lower()
    log.info(
        "{} {} {}{}{}.",
        f"{action.rstrip('e')}ing",
        len(objs),
        entity,
        "s" if len(objs) != 1 else "",
        unmoved_msg,
    )
    if confirm:
        objs = ui.input_select_objects(
            f"Really {action}?", objs, lambda o: o.show_path_change(dest)
        )

    for obj in objs:
        obj.show_path_change(dest)
        if not pretend:
            obj.move(operation=operation, basedir=dest, store=store)


def move_func(lib, opts, args):
    dest = opts.dest
    if dest is not None:
        dest = normpath(dest)
        if not os.path.isdir(syspath(dest)):
            raise UserError(f"no such directory: {displayable_path(dest)}")

    items, albums = _do_query(lib, args, opts.album, False)
    move_items(
        albums if opts.album else items,
        dest,
        MoveOperation.COPY if opts.copy or opts.export else MoveOperation.MOVE,
        opts.pretend,
        not opts.export,
        "album" if opts.album else "item",
        opts.timid,
    )


move_cmd = ui.Subcommand("move", help="move or copy items", aliases=("mv",))
move_cmd.parser.add_option(
    "-d", "--dest", metavar="DIR", dest="dest", help="destination directory"
)
move_cmd.parser.add_option(
    "-c",
    "--copy",
    default=False,
    action="store_true",
    help="copy instead of moving",
)
move_cmd.parser.add_option(
    "-p",
    "--pretend",
    default=False,
    action="store_true",
    help="show how files would be moved, but don't touch anything",
)
move_cmd.parser.add_option(
    "-t",
    "--timid",
    dest="timid",
    action="store_true",
    help="always confirm all actions",
)
move_cmd.parser.add_option(
    "-e",
    "--export",
    default=False,
    action="store_true",
    help="copy without changing the database path",
)
move_cmd.parser.add_album_option()
move_cmd.func = move_func
default_commands.append(move_cmd)


# write: Write tags into files.


def write_items(lib, query, pretend, force):
    """Write tag information from the database to the respective files
    in the filesystem.
    """
    items, albums = _do_query(lib, query, False, False)

    for item in items:
        # Item deleted?
        if not os.path.exists(syspath(item.path)):
            log.info("missing file: {.filepath}", item)
            continue

        # Get an Item object reflecting the "clean" (on-disk) state.
        try:
            clean_item = library.Item.from_path(item.path)
        except library.ReadError as exc:
            log.error("error reading {.filepath}: {}", item, exc)
            continue

        # Check for and display changes.
        changed = ui.show_model_changes(
            item, clean_item, library.Item._media_tag_fields, force
        )
        if (changed or force) and not pretend:
            # We use `try_sync` here to keep the mtime up to date in the
            # database.
            item.try_sync(True, False)


def write_func(lib, opts, args):
    write_items(lib, args, opts.pretend, opts.force)


write_cmd = ui.Subcommand("write", help="write tag information to files")
write_cmd.parser.add_option(
    "-p",
    "--pretend",
    action="store_true",
    help="show all changes but do nothing",
)
write_cmd.parser.add_option(
    "-f",
    "--force",
    action="store_true",
    help="write tags even if the existing tags match the database",
)
write_cmd.func = write_func
default_commands.append(write_cmd)


# config: Show and edit user configuration.


def config_func(lib, opts, args):
    # Make sure lazy configuration is loaded
    config.resolve()

    # Print paths.
    if opts.paths:
        filenames = []
        for source in config.sources:
            if not opts.defaults and source.default:
                continue
            if source.filename:
                filenames.append(source.filename)

        # In case the user config file does not exist, prepend it to the
        # list.
        user_path = config.user_config_path()
        if user_path not in filenames:
            filenames.insert(0, user_path)

        for filename in filenames:
            print_(displayable_path(filename))

    # Open in editor.
    elif opts.edit:
        config_edit()

    # Dump configuration.
    else:
        config_out = config.dump(full=opts.defaults, redact=opts.redact)
        if config_out.strip() != "{}":
            print_(config_out)
        else:
            print("Empty configuration")


def config_edit():
    """Open a program to edit the user configuration.
    An empty config file is created if no existing config file exists.
    """
    path = config.user_config_path()
    editor = util.editor_command()
    try:
        if not os.path.isfile(path):
            open(path, "w+").close()
        util.interactive_open([path], editor)
    except OSError as exc:
        message = f"Could not edit configuration: {exc}"
        if not editor:
            message += (
                ". Please set the VISUAL (or EDITOR) environment variable"
            )
        raise UserError(message)


config_cmd = ui.Subcommand("config", help="show or edit the user configuration")
config_cmd.parser.add_option(
    "-p",
    "--paths",
    action="store_true",
    help="show files that configuration was loaded from",
)
config_cmd.parser.add_option(
    "-e",
    "--edit",
    action="store_true",
    help="edit user configuration with $VISUAL (or $EDITOR)",
)
config_cmd.parser.add_option(
    "-d",
    "--defaults",
    action="store_true",
    help="include the default configuration",
)
config_cmd.parser.add_option(
    "-c",
    "--clear",
    action="store_false",
    dest="redact",
    default=True,
    help="do not redact sensitive fields",
)
config_cmd.func = config_func
default_commands.append(config_cmd)


# completion: print completion script


def print_completion(*args):
    for line in completion_script(default_commands + plugins.commands()):
        print_(line, end="")
    if not any(os.path.isfile(syspath(p)) for p in BASH_COMPLETION_PATHS):
        log.warning(
            "Warning: Unable to find the bash-completion package. "
            "Command line completion might not work."
        )


BASH_COMPLETION_PATHS = [
    b"/etc/bash_completion",
    b"/usr/share/bash-completion/bash_completion",
    b"/usr/local/share/bash-completion/bash_completion",
    # SmartOS
    b"/opt/local/share/bash-completion/bash_completion",
    # Homebrew (before bash-completion2)
    b"/usr/local/etc/bash_completion",
]


def completion_script(commands):
    """Yield the full completion shell script as strings.

    ``commands`` is alist of ``ui.Subcommand`` instances to generate
    completion data for.
    """
    base_script = os.path.join(os.path.dirname(__file__), "completion_base.sh")
    with open(base_script) as base_script:
        yield base_script.read()

    options = {}
    aliases = {}
    command_names = []

    # Collect subcommands
    for cmd in commands:
        name = cmd.name
        command_names.append(name)

        for alias in cmd.aliases:
            if re.match(r"^\w+$", alias):
                aliases[alias] = name

        options[name] = {"flags": [], "opts": []}
        for opts in cmd.parser._get_all_options()[1:]:
            if opts.action in ("store_true", "store_false"):
                option_type = "flags"
            else:
                option_type = "opts"

            options[name][option_type].extend(
                opts._short_opts + opts._long_opts
            )

    # Add global options
    options["_global"] = {
        "flags": ["-v", "--verbose"],
        "opts": "-l --library -c --config -d --directory -h --help".split(" "),
    }

    # Add flags common to all commands
    options["_common"] = {"flags": ["-h", "--help"]}

    # Start generating the script
    yield "_beet() {\n"

    # Command names
    yield f"  local commands={' '.join(command_names)!r}\n"
    yield "\n"

    # Command aliases
    yield f"  local aliases={' '.join(aliases.keys())!r}\n"
    for alias, cmd in aliases.items():
        yield f"  local alias__{alias.replace('-', '_')}={cmd}\n"
    yield "\n"

    # Fields
    fields = library.Item._fields.keys() | library.Album._fields.keys()
    yield f"  fields={' '.join(fields)!r}\n"

    # Command options
    for cmd, opts in options.items():
        for option_type, option_list in opts.items():
            if option_list:
                option_list = " ".join(option_list)
                yield (
                    "  local"
                    f" {option_type}__{cmd.replace('-', '_')}='{option_list}'\n"
                )

    yield "  _beet_dispatch\n"
    yield "}\n"


completion_cmd = ui.Subcommand(
    "completion",
    help="print shell script that provides command line completion",
)
completion_cmd.func = print_completion
completion_cmd.hide = True
default_commands.append(completion_cmd)
