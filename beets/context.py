from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

# Holds the music dir context
_music_dir_var: ContextVar[Path] = ContextVar("music_dir", default=Path())


def get_music_dir() -> Path:
    """Get the current music directory context."""
    return _music_dir_var.get()


def set_music_dir(value: Path) -> None:
    """Set the current music directory context."""
    _music_dir_var.set(value)


@contextmanager
def music_dir(value: Path):
    """Temporarily bind the active music directory for query parsing."""
    token = _music_dir_var.set(value)
    try:
        yield
    finally:
        _music_dir_var.reset(token)
