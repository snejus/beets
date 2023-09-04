from __future__ import annotations

import os
import re
from typing import Literal

from rich.text import Text

from beets import config

# ANSI terminal colorization code heavily inspired by pygments:
# https://bitbucket.org/birkenfeld/pygments-main/src/default/pygments/console.py
# (pygments is by Tim Hatch, Armin Ronacher, et al.)
COLOR_ESCAPE = "\x1b"
RESET_COLOR = f"{COLOR_ESCAPE}[39;49;00m"
# Precompile common ANSI-escape regex patterns
ANSI_CODE_REGEX = re.compile(rf"({COLOR_ESCAPE}\[[;0-9]*m)")
ESC_TEXT_REGEX = re.compile(
    rf"""(?P<pretext>[^{COLOR_ESCAPE}]*)
         (?P<esc>(?:{ANSI_CODE_REGEX.pattern})+)
         (?P<text>[^{COLOR_ESCAPE}]+)(?P<reset>{re.escape(RESET_COLOR)})
         (?P<posttext>[^{COLOR_ESCAPE}]*)""",
    re.VERBOSE,
)
ColorName = Literal[
    "text_success",
    "text_warning",
    "text_error",
    "text_highlight",
    "text_highlight_minor",
    "action_default",
    "action",
    # New Colors
    "text_faint",
    "import_path",
    "import_path_items",
    "action_description",
    "changed",
    "text_diff_added",
    "text_diff_removed",
]


def _colorize(color_name: ColorName, text: str) -> str:
    """Apply ANSI color formatting to text based on configuration settings."""
    color = " ".join(config["ui"]["colors"][color_name].as_str_seq())
    return f"[{color}]{text}[/{color}]"


def colorize(color_name: ColorName, text: str) -> str:
    """Colorize text when color output is enabled."""
    if config["ui"]["color"] and "NO_COLOR" not in os.environ:
        return _colorize(color_name, text)

    return text


def uncolorize(text: str) -> str:
    return Text(text).plain


def color_split(colored_text: str, index: int) -> tuple[str, str]:
    pre, post = [ln.markup for ln in Text(colored_text).divide((index,))]
    return pre, post


def color_len(colored_text: str) -> int:
    """Return the length of a string without color codes."""
    return len(uncolorize(colored_text))
