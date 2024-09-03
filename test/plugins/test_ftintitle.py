# This file is part of beets.
# Copyright 2016, Fabrice Laporte.
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

"""Tests for the 'ftintitle' plugin."""

import pytest

from beets.test.helper import PluginMixin, TestHelper
from beetsplug.ftintitle import find_feat_part

_p = pytest.param


class TestKickFtAround(PluginMixin, TestHelper):
    plugin = "ftintitle"

    ARTIST = "Alice"
    TITLE = "Title"
    ARTIST_WITH_FT = "Alice ft Bob"
    TITLE_WITH_FT = "Title feat. Bob"

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.setup_beets()

        yield

        self.teardown_beets()

    @pytest.fixture
    def item(self):
        return self.add_item_fixture(
            albumartist=self.ARTIST,
            artist=self.ARTIST_WITH_FT,
            title=self.TITLE,
        )

    @pytest.fixture
    def command(self, drop):
        return ("ftintitle", "--drop") if drop else ("ftintitle",)

    @pytest.mark.parametrize(
        "drop, keep_in_artist, expected_artist, expected_title",
        [
            _p(False, False, ARTIST, TITLE_WITH_FT, id="move-to-title"),
            _p(False, True, ARTIST_WITH_FT, TITLE_WITH_FT, id="copy-to-title"),
            _p(True, False, ARTIST, TITLE, id="drop"),
            _p(True, True, ARTIST_WITH_FT, TITLE, id="keep-in-place"),
        ],
    )
    def test_handle_ft(
        self, item, command, keep_in_artist, expected_artist, expected_title
    ):
        with self.configure_plugin({"keep_in_artist": keep_in_artist}):
            self.run_command(*command)

        item.load()
        assert item.artist == expected_artist
        assert item.title == expected_title


@pytest.mark.parametrize(
    "album_artist, artist, expected_feat_artist",
    [
        ("Alice", "Alice ft. Bob", "Bob"),
        ("Alice", "Alice & Bob", "Bob"),
        ("Alice", "Alice defeat Bob", None),
        ("Alice", "Bob ft. Carol", None),
        ("Alice ft. Bob", "Alice ft. Bob", None),
    ],
)
def test_find_feat_part(artist, album_artist, expected_feat_artist):
    assert find_feat_part(artist, album_artist) == expected_feat_artist
