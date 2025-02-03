from __future__ import annotations

from collections import Counter
from itertools import chain
from typing import TYPE_CHECKING, Any

from beets import config, importer, logging, plugins, ui
from beets.autotag import Recommendation
from beets.autotag.hooks import Match
from beets.util import PromptChoice, displayable_path
from beets.util.units import human_bytes

from .display import AlbumView, SingletonView, View

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.autotag.hooks import AnyMatch
    from beets.importer import Action
    from beets.importer.tasks import ImportTask
    from beets.library.models import AnyModel, Item

# Global logger.
log = logging.getLogger(__name__)


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
        self,
        task: importer.ImportTask[AnyMatch],
        duplicates: list[AnyModel],
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
            ui.print_("Old: " + summarize_items(dupes, not task.is_album))

            if config["import"]["duplicate_verbose_prompt"]:
                for dup in dupes:
                    print(f"  {dup}")

        items = task.imported_items()
        ui.print_("New: " + summarize_items(items, not task.is_album))

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


def summarize_items(items: list[Item], singleton: bool) -> str:
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
        ui.print_("Skipping.")
    elif action == importer.Action.ASIS:
        ui.print_("Importing as-is.")
    return action


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
        search_artist=ui.input_("Artist:").strip(),
        search_name=ui.input_("Album:" if task.is_album else "Track:").strip(),
    )


def manual_id(
    session: importer.ImportSession, task: importer.ImportTask[AnyMatch]
) -> None:
    """Update task with candidates using a manually-entered ID.

    Input an ID, either for an album ("release") or a track ("recording").
    """
    _type = "release" if task.is_album else "recording"
    task.lookup_candidates(
        search_ids=ui.input_(f"Enter {_type} ID:").strip().split()
    )


def abort_action(session, task):
    """A prompt choice callback that aborts the importer."""
    raise importer.ImportAbortError()
