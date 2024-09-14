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

"""Tests for the 'lyrics' plugin."""

import os
import re
from functools import partial
from http import HTTPStatus

import pytest

from beets.library import Item
from beets.test.helper import PluginMixin
from beetsplug import lyrics

PHRASE_BY_TITLE = {
    "Lady Madonna": "friday night arrives without a suitcase",
    "Jazz'n'blues": "as i check my balance i kiss the screen",
    "Beets song": "via plugins, beets becomes a panacea",
}

_p = pytest.param


skip_ci = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub actions is on some form of Cloudflare blacklist",
)


class TestLyricsUtils:
    def test_search_artist(self):
        item = Item(artist="Alice ft. Bob", title="song")
        assert ("Alice ft. Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) in lyrics.search_pairs(item)

        item = Item(artist="Alice feat Bob", title="song")
        assert ("Alice feat Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) in lyrics.search_pairs(item)

        item = Item(artist="Alice feat. Bob", title="song")
        assert ("Alice feat. Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) in lyrics.search_pairs(item)

        item = Item(artist="Alice feats Bob", title="song")
        assert ("Alice feats Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) not in lyrics.search_pairs(item)

        item = Item(artist="Alice featuring Bob", title="song")
        assert ("Alice featuring Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) in lyrics.search_pairs(item)

        item = Item(artist="Alice & Bob", title="song")
        assert ("Alice & Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) in lyrics.search_pairs(item)

        item = Item(artist="Alice and Bob", title="song")
        assert ("Alice and Bob", ["song"]) in lyrics.search_pairs(item)
        assert ("Alice", ["song"]) in lyrics.search_pairs(item)

        item = Item(artist="Alice and Bob", title="song")
        assert ("Alice and Bob", ["song"]) == list(lyrics.search_pairs(item))[0]

    def test_search_artist_sort(self):
        item = Item(artist="CHVRCHΞS", title="song", artist_sort="CHVRCHES")
        assert ("CHVRCHΞS", ["song"]) in lyrics.search_pairs(item)
        assert ("CHVRCHES", ["song"]) in lyrics.search_pairs(item)

        # Make sure that the original artist name is still the first entry
        assert ("CHVRCHΞS", ["song"]) == list(lyrics.search_pairs(item))[0]

        item = Item(
            artist="横山克", title="song", artist_sort="Masaru Yokoyama"
        )
        assert ("横山克", ["song"]) in lyrics.search_pairs(item)
        assert ("Masaru Yokoyama", ["song"]) in lyrics.search_pairs(item)

        # Make sure that the original artist name is still the first entry
        assert ("横山克", ["song"]) == list(lyrics.search_pairs(item))[0]

    def test_search_pairs_multi_titles(self):
        item = Item(title="1 / 2", artist="A")
        assert ("A", ["1 / 2"]) in lyrics.search_pairs(item)
        assert ("A", ["1", "2"]) in lyrics.search_pairs(item)

        item = Item(title="1/2", artist="A")
        assert ("A", ["1/2"]) in lyrics.search_pairs(item)
        assert ("A", ["1", "2"]) in lyrics.search_pairs(item)

    def test_search_pairs_titles(self):
        item = Item(title="Song (live)", artist="A")
        assert ("A", ["Song"]) in lyrics.search_pairs(item)
        assert ("A", ["Song (live)"]) in lyrics.search_pairs(item)

        item = Item(title="Song (live) (new)", artist="A")
        assert ("A", ["Song"]) in lyrics.search_pairs(item)
        assert ("A", ["Song (live) (new)"]) in lyrics.search_pairs(item)

        item = Item(title="Song (live (new))", artist="A")
        assert ("A", ["Song"]) in lyrics.search_pairs(item)
        assert ("A", ["Song (live (new))"]) in lyrics.search_pairs(item)

        item = Item(title="Song ft. B", artist="A")
        assert ("A", ["Song"]) in lyrics.search_pairs(item)
        assert ("A", ["Song ft. B"]) in lyrics.search_pairs(item)

        item = Item(title="Song featuring B", artist="A")
        assert ("A", ["Song"]) in lyrics.search_pairs(item)
        assert ("A", ["Song featuring B"]) in lyrics.search_pairs(item)

        item = Item(title="Song and B", artist="A")
        assert ("A", ["Song and B"]) in lyrics.search_pairs(item)
        assert ("A", ["Song"]) not in lyrics.search_pairs(item)

        item = Item(title="Song: B", artist="A")
        assert ("A", ["Song"]) in lyrics.search_pairs(item)
        assert ("A", ["Song: B"]) in lyrics.search_pairs(item)

    def test_remove_credits(self):
        assert (
            lyrics.remove_credits(
                """It's close to midnight
                                     Lyrics brought by example.com"""
            )
            == "It's close to midnight"
        )
        assert lyrics.remove_credits("""Lyrics brought by example.com""") == ""

        # don't remove 2nd verse for the only reason it contains 'lyrics' word
        text = """Look at all the shit that i done bought her
                  See lyrics ain't nothin
                  if the beat aint crackin"""
        assert lyrics.remove_credits(text) == text

    def test_scrape_strip_scripts(self):
        text = """foo<script>bar</script>baz"""
        assert lyrics._scrape_strip_cruft(text) == "foobaz"

    def test_scrape_merge_paragraphs(self):
        text = "one</p>   <p class='myclass'>two</p><p>three"
        assert lyrics._scrape_merge_paragraphs(text) == "one\ntwo\nthree"

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("test", "test"),
            ("Mørdag", "mordag"),
            ("l'été c'est fait pour jouer", "l-ete-c-est-fait-pour-jouer"),
            ("\xe7afe au lait (boisson)", "cafe-au-lait-boisson"),
            ("Multiple  spaces -- and symbols! -- merged", "multiple-spaces-and-symbols-merged"),  # noqa: E501
            ("\u200bno-width-space", "no-width-space"),
            ("El\u002dp", "el-p"),
            ("\u200bblackbear", "blackbear"),
            ("\u200d", ""),
            ("\u2010", ""),
        ],
    )  # fmt: skip
    def test_slug(self, text, expected):
        assert lyrics.slug(text) == expected


@pytest.fixture(scope="module")
def lyrics_root_dir(pytestconfig: pytest.Config):
    return pytestconfig.rootpath / "test" / "rsrc" / "lyrics"


class LyricsPluginMixin(PluginMixin):
    plugin = "lyrics"

    @pytest.fixture
    def plugin_config(self):
        """Return lyrics configuration to test."""
        return {}

    @pytest.fixture(autouse=True)
    def _setup_config(self, plugin_config):
        """Add plugin configuration to beets configuration."""
        self.config[self.plugin].set(plugin_config)


class LyricsPluginBackendMixin(LyricsPluginMixin):
    @pytest.fixture
    def backend(self, backend_name):
        """Return a lyrics backend instance."""
        return lyrics.LyricsPlugin().backends[backend_name]

    @pytest.fixture
    def lyrics_html(self, lyrics_root_dir, file_name):
        return (lyrics_root_dir / f"{file_name}.txt").read_text(
            encoding="utf-8"
        )

    @pytest.mark.integration_test
    def test_backend_source(self, backend):
        """Test default backends with a song known to exist in respective
        databases.
        """
        title = "Lady Madonna"
        res = backend.fetch("The Beatles", title)
        assert PHRASE_BY_TITLE[title] in res.lower()


class TestLyricsPlugin(LyricsPluginMixin):
    @pytest.fixture
    def plugin_config(self):
        """Return lyrics configuration to test."""
        return {"sources": ["lrclib"]}

    @pytest.mark.parametrize(
        "request_kwargs, expected_log_match",
        [
            (
                {"status_code": HTTPStatus.BAD_GATEWAY},
                r"LRCLib: Request error: 502",
            ),
            ({"text": "invalid"}, r"LRCLib: Could not decode.*JSON"),
        ],
    )
    def test_error_handling(
        self, requests_mock, caplog, request_kwargs, expected_log_match
    ):
        """Errors are logged with the plugin and backend name."""
        requests_mock.get(lyrics.LRCLib.base_url, **request_kwargs)

        plugin = lyrics.LyricsPlugin()
        assert plugin.get_lyrics("", "") is None
        assert caplog.messages
        last_log = caplog.messages[-1]
        assert last_log
        assert re.search(expected_log_match, last_log, re.I)


class TestGoogleLyrics(LyricsPluginBackendMixin):
    """Test scraping heuristics on a fake html page."""

    TITLE = "Beets song"

    @pytest.fixture(scope="class")
    def backend_name(self):
        return "google"

    @pytest.fixture(scope="class")
    def plugin_config(self):
        return {"google_API_key": "test"}

    @pytest.fixture(scope="class")
    def file_name(self):
        return "examplecom/beetssong"

    @pytest.mark.integration_test
    @pytest.mark.parametrize(
        "title, url",
        [
            *(
                ("Lady Madonna", url)
                for url in (
                    "http://www.chartlyrics.com/_LsLsZ7P4EK-F-LD4dJgDQ/Lady+Madonna.aspx",  # noqa: E501
                    "http://www.absolutelyrics.com/lyrics/view/the_beatles/lady_madonna",  # noqa: E501
                    "https://www.letras.mus.br/the-beatles/275/",
                    "https://www.lyricsmania.com/lady_madonna_lyrics_the_beatles.html",
                    "https://www.lyricsmode.com/lyrics/b/beatles/lady_madonna.html",
                    "https://www.paroles.net/the-beatles/paroles-lady-madonna",
                    "https://www.songlyrics.com/the-beatles/lady-madonna-lyrics/",
                    "https://sweetslyrics.com/the-beatles/lady-madonna-lyrics",
                    "https://www.musica.com/letras.asp?letra=59862",
                    "https://www.lacoccinelle.net/259956-the-beatles-lady-madonna.html",
                )
            ),
            pytest.param(
                "Lady Madonna",
                "https://www.azlyrics.com/lyrics/beatles/ladymadonna.html",
                marks=skip_ci,
            ),
            (
                "Jazz'n'blues",
                "https://www.lyricsontop.com/amy-winehouse-songs/jazz-n-blues-lyrics.html",  # noqa: E501
            ),
        ],
    )
    def test_backend_source(self, backend, title, url):
        """Test if lyrics present on websites registered in beets google custom
        search engine are correctly scraped.
        """
        response = backend.fetch_text(url)
        result = backend.scrape_lyrics(response).lower()

        assert backend.is_lyrics(result)
        assert PHRASE_BY_TITLE[title] in result

    def test_mocked_source_ok(self, backend, lyrics_html):
        """Test that lyrics of the mocked page are correctly scraped"""
        result = backend.scrape_lyrics(lyrics_html).lower()

        assert result
        assert backend.is_lyrics(result)
        assert PHRASE_BY_TITLE[self.TITLE] in result

    @pytest.mark.parametrize(
        "url_title, artist, should_be_candidate",
        [
            ("John Doe - beets song Lyrics", "John Doe", True),
            ("example.com | Beats song by John doe", "John Doe", True),
            ("example.com | seets bong lyrics by John doe", "John Doe", False),
            ("foo", "Sun O)))", False),
        ],
    )
    def test_is_page_candidate(
        self, backend, lyrics_html, url_title, artist, should_be_candidate
    ):
        result = backend.is_page_candidate(
            "http://www.example.com/lyrics/beetssong",
            url_title,
            self.TITLE,
            artist,
        )
        assert bool(result) == should_be_candidate

    @pytest.mark.parametrize(
        "lyrics",
        [
            "LyricsMania.com - Copyright (c) 2013 - All Rights Reserved",
            """All material found on this site is property\n
                     of mywickedsongtext brand""",
            """
Lyricsmania staff is working hard for you to add $TITLE lyrics as soon
as they'll be released by $ARTIST, check back soon!
In case you have the lyrics to $TITLE and want to send them to us, fill out
the following form.
""",
        ],
    )
    def test_bad_lyrics(self, backend, lyrics):
        assert not backend.is_lyrics(lyrics)


class TestGeniusLyrics(LyricsPluginBackendMixin):
    @pytest.fixture(scope="class")
    def backend_name(self):
        return "genius"

    @pytest.mark.parametrize(
        "file_name, expected_line_count",
        [
            ("geniuscom/2pacalleyezonmelyrics", 134),
            ("geniuscom/Ttngchinchillalyrics", 29),
            ("geniuscom/sample", 0),  # see https://github.com/beetbox/beets/issues/3535
        ],
    )  # fmt: skip
    def test_scrape_genius_lyrics(
        self, backend, lyrics_html, expected_line_count
    ):
        result = backend.scrape_lyrics(lyrics_html) or ""

        assert len(result.splitlines()) == expected_line_count


class TestTekstowoLyrics(LyricsPluginBackendMixin):
    @pytest.fixture(scope="class")
    def backend_name(self):
        return "tekstowo"

    @pytest.mark.parametrize(
        "file_name, query, should_scrape",
        [
            _p(
                "tekstowopl/piosenka24kgoldncityofangels1",
                ("24kGoldn", "City of Angels"),
                True,
                id="match",
            ),
            _p(
                "tekstowopl/piosenkabaileybiggerblackeyedsusan",
                ("Kelly Bailey", "Black Mesa Inbound"),
                False,
                id="no-search-query-match",
            ),
            _p(
                "tekstowopl/piosenkabeethovenbeethovenpianosonata17tempestthe3rdmovement",  # noqa: E501
                (
                    "Beethoven",
                    "Beethoven Piano Sonata 17 Tempest The 3rd Movement",
                ),
                False,
                id="no-lyrics",
            ),
        ],
    )
    def test_scrape_tekstowo_lyrics(
        self, backend, lyrics_html, query, should_scrape
    ):
        actually_scraped = bool(backend.scrape_lyrics(lyrics_html, *query))
        assert actually_scraped == should_scrape

    @pytest.mark.parametrize(
        "file_name, expected_url",
        [
            ("tekstowopl/szukajwykonawcaagfdgjatytulagfdgafg", None),
            (
                "tekstowopl/szukajwykonawcajuicewrldtytulluciddreams",
                "https://www.tekstowo.pl/piosenka,juice_wrld,lucid_dreams__remix__ft__lil_uzi_vert.html",  # noqa: E501
            ),
        ],
    )
    def test_parse_search_results(self, backend, lyrics_html, expected_url):
        assert backend.parse_search_results(lyrics_html) == expected_url


def lyrics_match(duration, synced, plain):
    return {"duration": duration, "syncedLyrics": synced, "plainLyrics": plain}


class TestLRCLibLyrics(LyricsPluginBackendMixin):
    ITEM_DURATION = 999

    @pytest.fixture(scope="class")
    def backend_name(self):
        return "lrclib"

    @pytest.fixture
    def response_data(self):
        return [lyrics_match(1, "synced", "plain")]

    @pytest.fixture
    def fetch_lyrics(self, backend, requests_mock, response_data):
        requests_mock.get(backend.base_url, json=response_data)

        return partial(backend.fetch, "la", "la", "la", self.ITEM_DURATION)

    @pytest.mark.parametrize(
        "response_data, expected_lyrics",
        [
            _p([], None, id="handle non-matching lyrics"),
            _p(
                [lyrics_match(1, "synced", "plain")],
                "synced",
                id="synced when available",
            ),
            _p(
                [lyrics_match(1, None, "plain")],
                "plain",
                id="plain by default",
            ),
            _p(
                [
                    lyrics_match(ITEM_DURATION, None, "plain 1"),
                    lyrics_match(1, "synced", "plain 2"),
                ],
                "plain 1",
                id="prefer matching duration",
            ),
            _p(
                [
                    lyrics_match(1, None, "plain 1"),
                    lyrics_match(1, "synced", "plain 2"),
                ],
                "synced",
                id="prefer match with synced lyrics",
            ),
        ],
    )
    @pytest.mark.parametrize("plugin_config", [{"synced": True}])
    def test_pick_lyrics_match(self, fetch_lyrics, expected_lyrics):
        assert fetch_lyrics() == expected_lyrics

    @pytest.mark.parametrize(
        "plugin_config, expected_lyrics",
        [({"synced": True}, "synced"), ({"synced": False}, "plain")],
    )
    def test_synced_config_option(self, fetch_lyrics, expected_lyrics):
        assert fetch_lyrics() == expected_lyrics
