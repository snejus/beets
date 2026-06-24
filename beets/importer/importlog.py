from __future__ import annotations

from beets import config, logging
from beets.exceptions import UserError
from beets.util import cached_classproperty, syspath

log = logging.getLogger(__name__)


class ImportLog:
    @cached_classproperty
    def importlog(cls) -> logging.Logger:
        """Get the logger for this class."""
        if not (view := config["import"]["log"]):
            return log

        path = syspath(view.as_filename())
        try:
            handler = logging.FileHandler(path, encoding="utf-8")
        except OSError as e:
            raise UserError(f"Could not open file for writing: {path}") from e

        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        log.propagate = True
        log.handlers.append(handler)
        return log
