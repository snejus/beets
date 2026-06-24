from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from typing_extensions import Self

from beets import config, library, logging, plugins, util
from beets.importer.tasks import Action
from beets.util import pipeline, syspath

from . import stages as stagefuncs
from .actions import DuplicateAction
from .importlog import ImportLog
from .state import ImportState

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    import confuse

    from beets import dbcore, library
    from beets.autotag import AlbumMatch, AnyMatch, TrackMatch
    from beets.library import AlbumOrItem
    from beets.util import PathBytes

    from .actions import Action
    from .tasks import ImportTask, SingletonImportTask


QUEUE_SIZE = 128

# Global logger.
log = logging.getLogger(__name__)


class ImportAbortError(Exception):
    """Raised when the user aborts the tagging operation."""


@dataclass
class ImportSession(ImportLog):
    """Controls an import action. Subclasses should implement methods to
    communicate with the user or otherwise make decisions.
    """

    lib: library.Library
    paths: list[PathBytes] = field(default_factory=list)
    query: str | Sequence[str] | dbcore.Query | None = None

    _is_resuming: dict[bytes, bool] = field(default_factory=dict, init=False)
    _merged_items: set[bytes] = field(default_factory=set, init=False)
    _merged_dirs: set[bytes] = field(default_factory=set, init=False)

    @classmethod
    def make(cls, *args, **kwargs) -> Self:
        kwargs["paths"] = kwargs.get("paths") or []
        return cls(*args, **kwargs)

    def set_config(self, config: confuse.ConfigView) -> None:
        """Set `config` property from global import config and make
        implied changes.
        """
        # FIXME: Maybe this function should not exist and should instead
        # provide "decision wrappers" like "should_resume()", etc.
        self.config = iconfig = config

        # Incremental and progress are mutually exclusive.
        if iconfig["incremental"]:
            iconfig["resume"] = False

        # When based on a query instead of directories, never
        # save progress or try to resume.
        if self.query is not None:
            iconfig["resume"] = False
            iconfig["incremental"] = False

        if iconfig["reflink"]:
            iconfig["reflink"] = iconfig["reflink"].as_choice(
                ["auto", True, False]
            )

        # Copy, move, reflink, link, and hardlink are mutually exclusive.
        if iconfig["move"]:
            iconfig["copy"] = False
            iconfig["link"] = False
            iconfig["hardlink"] = False
            iconfig["reflink"] = False
        elif iconfig["link"]:
            iconfig["copy"] = False
            iconfig["move"] = False
            iconfig["hardlink"] = False
            iconfig["reflink"] = False
        elif iconfig["hardlink"]:
            iconfig["copy"] = False
            iconfig["move"] = False
            iconfig["link"] = False
            iconfig["reflink"] = False
        elif iconfig["reflink"]:
            iconfig["copy"] = False
            iconfig["move"] = False
            iconfig["link"] = False
            iconfig["hardlink"] = False

        # Only delete when copying.
        if not iconfig["copy"]:
            iconfig["delete"] = False

        self.want_resume = config["resume"].as_choice([True, False, "ask"])

    def should_resume(self, path: PathBytes) -> bool:
        raise NotImplementedError

    def choose_match(self, task: ImportTask[AnyMatch]) -> AlbumMatch | Action:
        raise NotImplementedError

    def get_duplicate_action(
        self, task: ImportTask, found_duplicates: list[AlbumOrItem]
    ) -> DuplicateAction:
        """Get the configured duplicate action."""
        choice = config["import"]["duplicate_action"].as_choice(
            DuplicateAction.choices()
        )
        log.debug("default action for duplicates: {}", choice)
        return DuplicateAction(choice)  # type: ignore[call-arg]

    def choose_item(self, task: SingletonImportTask) -> TrackMatch | Action:
        raise NotImplementedError

    def run(self) -> None:
        """Run the import task."""
        self.importlog.info("import started {}", time.asctime())
        self.set_config(config["import"])

        stages: list[Iterator[stagefuncs.StageMessage]]
        # Set up the pipeline.
        if self.query is None:
            stages = [stagefuncs.read_tasks(self)]
        else:
            stages = [stagefuncs.query_tasks(self)]

        # In pretend mode, just log what would otherwise be imported.
        if self.config["pretend"]:
            stages += [stagefuncs.log_files(self)]
        else:
            if self.config["group_albums"] and not self.config["singletons"]:
                # Split directory tasks into one task for each album.
                stages += [stagefuncs.group_albums(self)]

            # These stages either talk to the user to get a decision or,
            # in the case of a non-autotagged import, just choose to
            # import everything as-is. In *both* cases, these stages
            # also add the music to the library database, so later
            # stages need to read and write data from there.
            if self.config["autotag"]:
                stages += [
                    stagefuncs.lookup_candidates(self),
                    stagefuncs.user_query(self),
                ]
            else:
                stages += [stagefuncs.import_asis(self)]

            # Plugin stages.
            for stage_func in plugins.early_import_stages():
                stages.append(stagefuncs.plugin_stage(self, stage_func))
            for stage_func in plugins.import_stages():
                stages.append(stagefuncs.plugin_stage(self, stage_func))

            stages += [stagefuncs.manipulate_files(self)]

        pl: pipeline.Pipeline[stagefuncs.StageMessage, stagefuncs.StageCoro] = (
            pipeline.Pipeline(stages)
        )

        # Run the pipeline.
        plugins.send("import_begin", session=self)
        try:
            if config["threaded"]:
                pl.run_parallel(QUEUE_SIZE)
            else:
                pl.run_sequential()
        except ImportAbortError:
            # User aborted operation. Silently stop.
            pass

    # Incremental and resumed imports

    def already_imported(
        self, toppath: PathBytes, paths: Sequence[PathBytes]
    ) -> bool:
        """Returns true if the files belonging to this task have already
        been imported in a previous session.
        """
        if self.is_resuming(toppath) and all(
            [ImportState().progress_has_element(toppath, p) for p in paths]
        ):
            return True
        if self.config["incremental"] and tuple(paths) in self.history_dirs:
            return True

        return False

    _history_dirs = None

    @property
    def history_dirs(self) -> set[tuple[PathBytes, ...]]:
        # FIXME: This could be simplified to a cached property
        if self._history_dirs is None:
            self._history_dirs = ImportState().taghistory
        return self._history_dirs

    def already_merged(self, paths: Sequence[PathBytes]) -> bool:
        """Returns true if all the paths being imported were part of a merge
        during previous tasks.
        """
        for path in paths:
            if path not in self._merged_items and path not in self._merged_dirs:
                return False
        return True

    def mark_merged(self, paths: Sequence[PathBytes]) -> None:
        """Mark paths and directories as merged for future reimport tasks."""
        self._merged_items.update(paths)
        dirs = {
            os.path.dirname(path) if os.path.isfile(syspath(path)) else path
            for path in paths
        }
        self._merged_dirs.update(dirs)

    def is_resuming(self, toppath: PathBytes) -> bool:
        """Return `True` if user wants to resume import of this path.

        You have to call `ask_resume` first to determine the return value.
        """
        return self._is_resuming.get(toppath, False)

    def ask_resume(self, toppath: PathBytes) -> None:
        """If import of `toppath` was aborted in an earlier session, ask
        user if they want to resume the import.

        Determines the return value of `is_resuming(toppath)`.
        """
        if self.want_resume and ImportState().progress_has(toppath):
            # Either accept immediately or prompt for input to decide.
            if self.want_resume is True or self.should_resume(toppath):
                log.warning(
                    "Resuming interrupted import of {}",
                    util.displayable_path(toppath),
                )
                self._is_resuming[toppath] = True
            else:
                # Clear progress; we're starting from the top.
                ImportState().progress_reset(toppath)
