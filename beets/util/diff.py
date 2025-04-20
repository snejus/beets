from __future__ import annotations

from typing import TYPE_CHECKING

from rich_tables.diff import diff

from beets import config

from .color import colorize

if TYPE_CHECKING:
    from collections.abc import Iterable

    from beets.library.models import FormattedMapping, LibModel


def colordiff(a: str, b: str) -> str | tuple[str, str]:
    """Intelligently highlight the differences between two strings."""
    if config["ui"]["color"]:
        return diff(a, b)

    return str(a), str(b)


FLOAT_EPSILON = 0.01


def _multi_value_diff(field: str, oldset: set[str], newset: set[str]) -> str:
    added = newset - oldset
    removed = oldset - newset

    parts = [
        f"{field}:",
        *(colorize("text_diff_removed", f"  - {i}") for i in sorted(removed)),
        *(colorize("text_diff_added", f"  + {i}") for i in sorted(added)),
    ]
    return "\n".join(parts)


def _field_diff(
    field: str, old: FormattedMapping, new: FormattedMapping
) -> str | None:
    """Given two Model objects and their formatted views, format their values
    for `field` and highlight changes among them. Return a human-readable
    string. If the value has not changed, return None instead.
    """
    # If no change, abort.
    if (oldval := old.model.get(field)) == (newval := new.model.get(field)) or (
        isinstance(oldval, float)
        and isinstance(newval, float)
        and abs(oldval - newval) < FLOAT_EPSILON
    ):
        return None

    if isinstance(oldval, list):
        if (oldset := set(oldval)) != (newset := set(newval)):
            return _multi_value_diff(field, oldset, newset)
        return None

    # Get formatted values for output.
    oldstr, newstr = old.get(field, ""), new.get(field, "")
    if field not in new:
        return colorize("text_diff_removed", f"{field}: {oldstr}")

    if field not in old:
        return colorize("text_diff_added", f"{field}: {newstr}")

    # For strings, highlight changes. For others, colorize the whole thing.
    if isinstance(oldval, str):
        out = colordiff(oldstr, newstr)
        if isinstance(out, str):
            oldstr, newstr = "", out
        else:
            oldstr, newstr = out
    else:
        oldstr = colorize("text_diff_removed", oldstr)
        newstr = colorize("text_diff_added", newstr)

    out = f"{field}: "
    if oldstr:
        out += f"{oldstr} -> "
    out += newstr
    return out


def get_model_changes(
    new: LibModel,
    old: LibModel,
    fields: Iterable[str] | None,
) -> list[str]:
    """Compute human-readable diff lines for changed fields between two models.

    Compares ``old`` and ``new`` across fixed and flex fields, excluding
    internal ones like ``mtime``. If ``fields`` is provided, only the
    specified subset is considered.
    """
    # Keep the formatted views around instead of re-creating them in each
    # iteration step
    old_fmt = old.formatted()
    new_fmt = new.formatted()

    # Build up lines showing changed fields.
    diff_fields = (set(old) | set(new)) - {"mtime"}
    if allowed_fields := set(fields or {}):
        diff_fields &= allowed_fields

    return [
        d
        for f in sorted(diff_fields)
        if (d := _field_diff(f, old_fmt, new_fmt))
    ]
