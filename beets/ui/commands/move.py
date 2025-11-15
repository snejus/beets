"""The 'move' command: Move/copy files to the library or a new base directory."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from beets import logging, ui
from beets.util import (
    MoveOperation,
    displayable_path,
    get_console,
    normpath,
    syspath,
)

from .utils import do_query

if TYPE_CHECKING:
    from beets.util import PathLike

# Global logger.
log = logging.getLogger(__name__)


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
    sources = list(map(displayable_path, sources))
    destinations = list(map(displayable_path, destinations))

    for source, dest in zip(sources, destinations):
        get_console().print(ui.colordiff(source, dest), highlight=False)


def move_items(
    lib,
    dest_path: PathLike,
    query,
    operation: MoveOperation,
    album,
    pretend,
    store: bool,
    confirm=False,
):
    """Moves or copies items to a new base directory, given by dest. If
    dest is None, then the library's base directory is used, making the
    command "consolidate" files.
    """
    dest = os.fsencode(dest_path) if dest_path else dest_path
    items, albums = do_query(lib, query, album, False)
    objs = albums if album else items
    num_objs = len(objs)

    # Filter out files that don't need to be moved.
    def isitemmoved(item):
        return item.path != item.destination(basedir=dest)

    def isalbummoved(album):
        return any(isitemmoved(i) for i in album.items())

    objs = [o for o in objs if (isalbummoved if album else isitemmoved)(o)]
    num_unmoved = num_objs - len(objs)
    # Report unmoved files that match the query.
    unmoved_msg = ""
    if num_unmoved > 0:
        unmoved_msg = f" ({num_unmoved} already in place)"

    entity = "album" if album else "item"
    action = operation.name.lower()
    log.info(
        "{} {} {}{}{}.",
        f"{action.rstrip('e')}ing",
        len(objs),
        entity,
        "s" if len(objs) != 1 else "",
        unmoved_msg,
    )
    if not objs:
        return

    if pretend:
        if album:
            show_path_changes(
                [
                    (item.path, item.destination(basedir=dest))
                    for obj in objs
                    for item in obj.items()
                ]
            )
        else:
            show_path_changes(
                [(obj.path, obj.destination(basedir=dest)) for obj in objs]
            )
    else:
        if confirm:
            objs = ui.input_select_objects(
                f"Really {action}?",
                objs,
                lambda o: show_path_changes(
                    [(o.path, o.destination(basedir=dest))]
                ),
            )

        for obj in objs:
            show_path_changes([(obj.path, obj.destination(basedir=dest))])

            obj.move(operation=operation, basedir=dest, store=store)


def move_func(lib, opts, args):
    dest = opts.dest
    if dest is not None:
        dest = normpath(dest)
        if not os.path.isdir(syspath(dest)):
            raise ui.UserError(f"no such directory: {displayable_path(dest)}")

    move_items(
        lib,
        dest,
        args,
        MoveOperation.COPY if opts.copy or opts.export else MoveOperation.MOVE,
        opts.album,
        opts.pretend,
        not opts.export,
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
