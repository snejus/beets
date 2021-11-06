# -*- coding: utf-8 -*-
# This file is part of beets.
# Copyright 2016, Dang Mai <contact@dangmai.net>.
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

"""Generates smart playlists based on beets queries.
"""

from __future__ import absolute_import, division, print_function

import os
from typing import Dict, Set

import six
from beets import ui
from beets.dbcore import OrQuery
from beets.dbcore.query import MultipleSort, ParsingError
from beets.library import Album, Item, parse_query_string
from beets.plugins import BeetsPlugin
from beets.util import bytestring_path, mkdirall, normpath, path_as_posix, syspath

try:
    from urllib.request import pathname2url
except ImportError:
    # python2 is a bit different
    from urllib import pathname2url


def timeit(method, **kwargs):
    def timed(*args, **kw):
        import time

        name = method.__name__
        ts = time.monotonic()
        result = method(*args, **kw)
        te = time.monotonic()
        if "log_time" in kw:
            name = kw.get("log_name", method.__name__.upper())
            kw["log_time"][name] = int((te - ts) * 1000)
        else:
            print(
                "{:<10.2f} ms  {} ({})".format((te - ts) * 1000, name, kwargs)
            )
        return result

    return timed


class SmartPlaylistPlugin(BeetsPlugin):
    def __init__(self):
        super(SmartPlaylistPlugin, self).__init__()
        self.config.add(
            {
                "relative_to": None,
                "playlist_dir": u".",
                "auto": True,
                "playlists": [],
                "forward_slash": False,
                "prefix": u"",
                "urlencode": False,
                "rewrite": False,
            }
        )

        self.config["prefix"].redact = True  # May contain username/password.
        self._matched_playlists = None
        self._unmatched_playlists = None

        if self.config["auto"]:
            self.register_listener("database_change", self.db_change)

    def commands(self):
        spl_update = ui.Subcommand(
            "splupdate",
            help=u"update the smart playlists. Playlist names may be "
            u"passed as arguments.",
        )
        spl_update.func = self.update_cmd
        return [spl_update]

    def update_cmd(self, lib, opts, args):
        timeit(self.build_queries)()
        if args:
            args = set(ui.decargs(args))
            for a in list(args):
                if not a.endswith(".m3u"):
                    args.add("{0}.m3u".format(a))

            playlists = set(
                (name, q, a_q)
                for name, q, a_q in self._unmatched_playlists
                if name in args
            )
            if not playlists:
                raise ui.UserError(
                    u"No playlist matching any of {0} found".format(
                        [name for name, _, _ in self._unmatched_playlists]
                    )
                )

            self._matched_playlists = playlists
            self._unmatched_playlists -= playlists
        else:
            self._matched_playlists = self._unmatched_playlists

        self.update_playlists(lib)

    def build_queries(self):
        """
        Instantiate queries for the playlists.

        Each playlist has 2 queries: one or items one for albums, each with a
        sort. We must also remember its name. _unmatched_playlists is a set of
        tuples (name, (q, q_sort), (album_q, album_q_sort)).

        sort may be any sort, or NullSort, or None. None and NullSort are
        equivalent and both eval to False.
        More precisely
        - it will be NullSort when a playlist query ('query' or 'album_query')
          is a single item or a list with 1 element
        - it will be None when there are multiple items i a query
        """
        self._unmatched_playlists = set()
        self._matched_playlists = set()

        for playlist in self.config["playlists"].get(list):
            if "name" not in playlist:
                self._log.warning(u"playlist configuration is missing name")
                continue

            playlist_data = (playlist["name"],)
            try:
                for key, model_cls in (("query", Item), ("album_query", Album)):
                    qs = playlist.get(key)
                    if qs is None:
                        query_and_sort = None, None
                    elif isinstance(qs, six.string_types):
                        query_and_sort = parse_query_string(qs, model_cls)
                    elif len(qs) == 1:
                        query_and_sort = parse_query_string(qs[0], model_cls)
                    else:
                        # multiple queries and sorts
                        queries, sorts = zip(
                            *(parse_query_string(q, model_cls) for q in qs)
                        )
                        query = OrQuery(queries)
                        final_sorts = []
                        for s in sorts:
                            if s:
                                if isinstance(s, MultipleSort):
                                    final_sorts += s.sorts
                                else:
                                    final_sorts.append(s)
                        if not final_sorts:
                            sort = None
                        elif len(final_sorts) == 1:
                            (sort,) = final_sorts
                        else:
                            sort = MultipleSort(final_sorts)
                        query_and_sort = query, sort

                    playlist_data += (query_and_sort,)

            except ParsingError as exc:
                self._log.warning(
                    u"invalid query in playlist {}", playlist["name"], exc_info=True
                )
                del exc
                continue

            self._unmatched_playlists.add(playlist_data)

    def matches(self, model, query, album_query):
        if album_query and isinstance(model, Album):
            return album_query.match(model)
        if query and isinstance(model, Item):
            return query.match(model)
        return False

    def db_change(self, lib, model):
        if self._unmatched_playlists is None:
            self.build_queries()

        for playlist in self._unmatched_playlists:
            n, (q, _), (a_q, _) = playlist
            if self.matches(model, q, a_q):
                self._log.debug(u"{0} will be updated because of {1}", n, model)
                self._matched_playlists.add(playlist)
                self.register_listener("cli_exit", self.update_playlists)

        self._unmatched_playlists -= self._matched_playlists

    def update_playlists(self, lib):
        self._log.info(u"Updating {0} smart playlists...", len(self._matched_playlists))

        playlist_dir = self.config["playlist_dir"].as_filename()
        playlist_dir = bytestring_path(playlist_dir)
        relative_to = self.config["relative_to"].get()
        if relative_to:
            relative_to = normpath(relative_to)

        # Maps playlist filenames to lists of track filenames.
        playlists_files: Dict[str, Set[str]] = {}

        for playlist in self._matched_playlists:
            playlist_name, (query, q_sort), (album_query, a_q_sort) = playlist
            playlists_files[playlist_name] = set()
            self._log.debug(u"Querying playlist {0}", playlist_name)

            def add_items():
                items = []
                if query:
                    items.extend(lib.items(query, q_sort))
                if album_query:
                    for album in lib.albums(album_query, a_q_sort):
                        items.extend(album.items())
                return items

            # items = timeit(add_items, name=playlist_name, query=query)()
            items = add_items()

            # As we allow tags in the m3u names, we'll need to iterate through
            # the items and generate the correct m3u file names.
            prefix = bytestring_path(self.config["prefix"].as_str())
            for item in items:
                item_path = item.path
                if relative_to:
                    item_path = os.path.relpath(item_path, relative_to)
                if self.config["forward_slash"].get():
                    item_path = path_as_posix(item_path)
                if self.config["urlencode"]:
                    item_path = bytestring_path(pathname2url(item_path))
                playlists_files[playlist_name].add(prefix + item_path)

        # Write all of the accumulated track lists to files.
        updated_count = 0
        mkdirall(playlist_dir)
        for m3u in playlists_files:
            new_playlist = sorted(
                playlists_files[m3u], key=lambda x: x.decode().casefold()
            )
            m3u_path = syspath(normpath(os.path.join(playlist_dir, bytestring_path(m3u))))
            if self.config["rewrite"].get() or not os.path.exists(m3u_path):
                current_playlist = []
            else:
                current_playlist = list(
                    map(str.encode, map(str.strip, open(m3u_path, "r").readlines()))
                )

            if current_playlist != new_playlist:
                updated_count = updated_count + 1
                lendiff = len(new_playlist) - len(current_playlist)
                if lendiff > 0:
                    logmsg = "Adding {} new choones to the {} playlist"
                elif lendiff < 0:
                    logmsg = "Removing {} choones from the {} playlist"
                else:
                    lendiff = len(
                        set(new_playlist).symmetric_difference(set(current_playlist))
                    )
                    logmsg = "Updating {} choones in the {} playlist"

                with open(m3u_path, "wb") as f:
                    self._log.info(logmsg, abs(lendiff), m3u)
                    f.write(b"\n".join(new_playlist))

        self._log.info(u"{0} playlists updated", updated_count)
