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

import itertools
import os
import re
import unittest
from unittest.mock import MagicMock, patch

import pytest
import requests

from beets import logging
from beets.library import Item
from beets.test import _common
from beets.util import bytestring_path
from beetsplug import lyrics

PHRASE_BY_TITLE = {
    "Lady Madonna": "friday night arrives without a suitcase",
    "Jazz'n'blues": "as i check my balance i kiss the screen",
    "Beets song": "via plugins, beets becomes a panacea",
}

log = logging.getLogger("beets.test_lyrics")
raw_backend = lyrics.Backend({}, log)
google = lyrics.Google(MagicMock(), log)
genius = lyrics.Genius(MagicMock(), log)
tekstowo = lyrics.Tekstowo(MagicMock(), log)
lrclib = lyrics.LRCLib(MagicMock(), log)


def xfail_on_ci(msg: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(
        bool(os.environ.get("GITHUB_ACTIONS")),
        reason=msg,
        raises=AssertionError,
    )


class LyricsPluginTest(unittest.TestCase):
    def setUp(self):
        """Set up configuration."""
        lyrics.LyricsPlugin()

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

    def test_is_lyrics(self):
        texts = ["LyricsMania.com - Copyright (c) 2013 - All Rights Reserved"]
        texts += [
            """All material found on this site is property\n
                     of mywickedsongtext brand"""
        ]
        for t in texts:
            assert not google.is_lyrics(t)

    def test_slugify(self):
        text = "http://site.com/\xe7afe-au_lait(boisson)"
        assert google.slugify(text) == "http://site.com/cafe_au_lait"

    def test_scrape_strip_cruft(self):
        text = """<!--lyrics below-->
                  &nbsp;one
                  <br class='myclass'>
                  two  !
                  <br><br \\>
                  <blink>four</blink>"""
        assert lyrics._scrape_strip_cruft(text, True) == "one\ntwo !\n\nfour"

    def test_scrape_strip_scripts(self):
        text = """foo<script>bar</script>baz"""
        assert lyrics._scrape_strip_cruft(text, True) == "foobaz"

    def test_scrape_strip_tag_in_comment(self):
        text = """foo<!--<bar>-->qux"""
        assert lyrics._scrape_strip_cruft(text, True) == "fooqux"

    def test_scrape_merge_paragraphs(self):
        text = "one</p>   <p class='myclass'>two</p><p>three"
        assert lyrics._scrape_merge_paragraphs(text) == "one\ntwo\nthree"

    def test_missing_lyrics(self):
        lyrics = """
Lyricsmania staff is working hard for you to add $TITLE lyrics as soon
as they'll be released by $ARTIST, check back soon!
In case you have the lyrics to $TITLE and want to send them to us, fill out
the following form.
"""
        assert not google.is_lyrics(lyrics)


def url_to_filename(url):
    url = re.sub(r"https?://|www.", "", url)
    url = re.sub(r".html", "", url)
    fn = "".join(x for x in url if (x.isalnum() or x == "/"))
    fn = fn.split("/")
    fn = os.path.join(
        LYRICS_ROOT_DIR,
        bytestring_path(fn[0]),
        bytestring_path(fn[-1] + ".txt"),
    )
    return fn


class MockFetchUrl:
    def __init__(self, pathval="fetched_path"):
        self.pathval = pathval
        self.fetched = None

    def __call__(self, url, filename=None):
        self.fetched = url
        fn = url_to_filename(url)
        with open(fn, encoding="utf8") as f:
            content = f.read()
        return content


LYRICS_ROOT_DIR = os.path.join(_common.RSRC, b"lyrics")


class LyricsGoogleBaseTest(unittest.TestCase):
    def setUp(self):
        """Set up configuration."""
        try:
            __import__("bs4")
        except ImportError:
            self.skipTest("Beautiful Soup 4 not available")


@pytest.mark.integration_test
class TestSources:
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
                marks=xfail_on_ci("AZLyrics is blocked by Cloudflare"),
            ),
            (
                "Jazz'n'blues",
                "https://www.lyricsontop.com/amy-winehouse-songs/jazz-n-blues-lyrics.html",  # noqa: E501
            ),
        ],
    )
    def test_google_source(self, title, url):
        """Test if lyrics present on websites registered in beets google custom
        search engine are correctly scraped.
        """
        response = raw_backend.fetch_url(url)
        result = lyrics.scrape_lyrics_from_html(response).lower()

        assert google.is_lyrics(result)
        assert PHRASE_BY_TITLE[title] in result

    @pytest.mark.parametrize(
        "backend",
        [
            pytest.param(
                lyrics.Genius,
                marks=xfail_on_ci(
                    "GitHub actions is on some form of Cloudflare blacklist"
                ),
            ),
            lyrics.Tekstowo,
            lyrics.LRCLib,
            # lyrics.MusiXmatch,
        ],
    )
    def test_backend_source(self, backend):
        """Test default backends with a song known to exist in respective
        databases.
        """
        plugin = lyrics.LyricsPlugin()
        backend = backend(plugin.config, plugin._log)
        title = "Lady Madonna"
        res = backend.fetch("The Beatles", title)
        assert PHRASE_BY_TITLE[title] in res.lower()


class LyricsGooglePluginMachineryTest(LyricsGoogleBaseTest):
    """Test scraping heuristics on a fake html page."""

    source = dict(
        url="http://www.example.com",
        artist="John Doe",
        title="Beets song",
        path="/lyrics/beetssong",
    )

    def setUp(self):
        """Set up configuration"""
        LyricsGoogleBaseTest.setUp(self)
        self.plugin = lyrics.LyricsPlugin()

    @patch.object(lyrics.Backend, "fetch_url", MockFetchUrl())
    def test_mocked_source_ok(self):
        """Test that lyrics of the mocked page are correctly scraped"""
        url = self.source["url"] + self.source["path"]
        res = lyrics.scrape_lyrics_from_html(raw_backend.fetch_url(url))
        assert google.is_lyrics(res), url
        assert PHRASE_BY_TITLE[self.source["title"]] in res.lower()

    @patch.object(lyrics.Backend, "fetch_url", MockFetchUrl())
    def test_is_page_candidate_exact_match(self):
        """Test matching html page title with song infos -- when song infos are
        present in the title.
        """
        from bs4 import BeautifulSoup, SoupStrainer

        s = self.source
        url = str(s["url"] + s["path"])
        html = raw_backend.fetch_url(url)
        soup = BeautifulSoup(
            html, "html.parser", parse_only=SoupStrainer("title")
        )
        assert google.is_page_candidate(
            url, soup.title.string, s["title"], s["artist"]
        ), url

    def test_is_page_candidate_fuzzy_match(self):
        """Test matching html page title with song infos -- when song infos are
        not present in the title.
        """
        s = self.source
        url = s["url"] + s["path"]
        url_title = "example.com | Beats song by John doe"

        # very small diffs (typo) are ok eg 'beats' vs 'beets' with same artist
        assert google.is_page_candidate(
            url, url_title, s["title"], s["artist"]
        ), url
        # reject different title
        url_title = "example.com | seets bong lyrics by John doe"
        assert not google.is_page_candidate(
            url, url_title, s["title"], s["artist"]
        ), url

    def test_is_page_candidate_special_chars(self):
        """Ensure that `is_page_candidate` doesn't crash when the artist
        and such contain special regular expression characters.
        """
        # https://github.com/beetbox/beets/issues/1673
        s = self.source
        url = s["url"] + s["path"]
        url_title = "foo"

        google.is_page_candidate(url, url_title, s["title"], "Sunn O)))")


# test Genius backend


class GeniusBaseTest(unittest.TestCase):
    def setUp(self):
        """Set up configuration."""
        try:
            __import__("bs4")
        except ImportError:
            self.skipTest("Beautiful Soup 4 not available")


class GeniusScrapeLyricsFromHtmlTest(GeniusBaseTest):
    """tests Genius._scrape_lyrics_from_html()"""

    def setUp(self):
        """Set up configuration"""
        GeniusBaseTest.setUp(self)
        self.plugin = lyrics.LyricsPlugin()

    def test_no_lyrics_div(self):
        """Ensure we don't crash when the scraping the html for a genius page
        doesn't contain <div class="lyrics"></div>
        """
        # https://github.com/beetbox/beets/issues/3535
        # expected return value None
        url = "https://genius.com/sample"
        mock = MockFetchUrl()
        assert genius._scrape_lyrics_from_html(mock(url)) is None

    def test_good_lyrics(self):
        """Ensure we are able to scrape a page with lyrics"""
        url = "https://genius.com/Ttng-chinchilla-lyrics"
        mock = MockFetchUrl()
        lyrics = genius._scrape_lyrics_from_html(mock(url))
        assert lyrics is not None
        assert lyrics.count("\n") == 28

    def test_good_lyrics_multiple_divs(self):
        """Ensure we are able to scrape a page with lyrics"""
        url = "https://genius.com/2pac-all-eyez-on-me-lyrics"
        mock = MockFetchUrl()
        lyrics = genius._scrape_lyrics_from_html(mock(url))
        assert lyrics is not None
        assert lyrics.count("\n") == 133

    # TODO: find an example of a lyrics page with multiple divs and test it


class GeniusFetchTest(GeniusBaseTest):
    """tests Genius.fetch()"""

    def setUp(self):
        """Set up configuration"""
        GeniusBaseTest.setUp(self)
        self.plugin = lyrics.LyricsPlugin()

    @patch.object(lyrics.Genius, "_scrape_lyrics_from_html")
    @patch.object(lyrics.Backend, "fetch_url", return_value=True)
    def test_json(self, mock_fetch_url, mock_scrape):
        """Ensure we're finding artist matches"""
        with patch.object(
            lyrics.Genius,
            "_search",
            return_value={
                "response": {
                    "hits": [
                        {
                            "result": {
                                "primary_artist": {
                                    "name": "\u200bblackbear",
                                },
                                "url": "blackbear_url",
                            }
                        },
                        {
                            "result": {
                                "primary_artist": {"name": "El\u002dp"},
                                "url": "El-p_url",
                            }
                        },
                    ]
                }
            },
        ) as mock_json:
            # genius uses zero-width-spaces (\u200B) for lowercase
            # artists so we make sure we can match those
            assert genius.fetch("blackbear", "Idfc") is not None
            mock_fetch_url.assert_called_once_with("blackbear_url")
            mock_scrape.assert_called_once_with(True)

            # genius uses the hyphen minus (\u002D) as their dash
            assert genius.fetch("El-p", "Idfc") is not None
            mock_fetch_url.assert_called_with("El-p_url")
            mock_scrape.assert_called_with(True)

            # test no matching artist
            assert genius.fetch("doesntexist", "none") is None

            # test invalid json
            mock_json.return_value = None
            assert genius.fetch("blackbear", "Idfc") is None


# test Tekstowo


class TekstowoBaseTest(unittest.TestCase):
    def setUp(self):
        """Set up configuration."""
        try:
            __import__("bs4")
        except ImportError:
            self.skipTest("Beautiful Soup 4 not available")


class TekstowoExtractLyricsTest(TekstowoBaseTest):
    """tests Tekstowo.extract_lyrics()"""

    def setUp(self):
        """Set up configuration"""
        TekstowoBaseTest.setUp(self)
        self.plugin = lyrics.LyricsPlugin()
        tekstowo.config = self.plugin.config

    def test_good_lyrics(self):
        """Ensure we are able to scrape a page with lyrics"""
        url = "https://www.tekstowo.pl/piosenka,24kgoldn,city_of_angels_1.html"
        mock = MockFetchUrl()
        assert tekstowo.extract_lyrics(mock(url))

    def test_no_lyrics(self):
        """Ensure we don't crash when the scraping the html for a Tekstowo page
        doesn't contain lyrics
        """
        url = (
            "https://www.tekstowo.pl/piosenka,beethoven,"
            "beethoven_piano_sonata_17_tempest_the_3rd_movement.html"
        )
        mock = MockFetchUrl()
        assert not tekstowo.extract_lyrics(mock(url))


# test LRCLib backend


class LRCLibLyricsTest(unittest.TestCase):
    def setUp(self):
        self.plugin = lyrics.LyricsPlugin()
        lrclib.config = self.plugin.config

    @patch("beetsplug.lyrics.requests.get")
    def test_fetch_synced_lyrics(self, mock_get):
        mock_response = {
            "syncedLyrics": "[00:00.00] la la la",
            "plainLyrics": "la la la",
        }
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.status_code = 200

        self.plugin.config["synced"] = False
        lyrics = lrclib.fetch("la", "la", "la", 999)
        assert lyrics == mock_response["plainLyrics"]

        self.plugin.config["synced"] = True
        lyrics = lrclib.fetch("la", "la", "la", 999)
        assert lyrics == mock_response["syncedLyrics"]

    @patch("beetsplug.lyrics.requests.get")
    def test_fetch_synced_lyrics_fallback(self, mock_get):
        mock_response = {
            "syncedLyrics": "",
            "plainLyrics": "la la la",
        }
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.status_code = 200

        self.plugin.config["synced"] = True
        lyrics = lrclib.fetch("la", "la", "la", 999)
        assert lyrics == mock_response["plainLyrics"]

    @patch("beetsplug.lyrics.requests.get")
    def test_fetch_plain_lyrics(self, mock_get):
        mock_response = {
            "syncedLyrics": "",
            "plainLyrics": "la la la",
        }
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.status_code = 200

        self.plugin.config["synced"] = False
        lyrics = lrclib.fetch("la", "la", "la", 999)

        assert lyrics == mock_response["plainLyrics"]

    @patch("beetsplug.lyrics.requests.get")
    def test_fetch_not_found(self, mock_get):
        mock_response = {
            "statusCode": 404,
            "error": "Not Found",
            "message": "Failed to find specified track",
        }
        mock_get.return_value.json.return_value = mock_response
        mock_get.return_value.status_code = 404

        lyrics = lrclib.fetch("la", "la", "la", 999)

        assert lyrics is None

    @patch("beetsplug.lyrics.requests.get")
    def test_fetch_exception(self, mock_get):
        mock_get.side_effect = requests.RequestException

        lyrics = lrclib.fetch("la", "la", "la", 999)

        assert lyrics is None


# test utilities


class SlugTests(unittest.TestCase):
    def test_slug(self):
        # plain ascii passthrough
        text = "test"
        assert lyrics.slug(text) == "test"

        # german unicode and capitals
        text = "Mørdag"
        assert lyrics.slug(text) == "mordag"

        # more accents and quotes
        text = "l'été c'est fait pour jouer"
        assert lyrics.slug(text) == "l-ete-c-est-fait-pour-jouer"

        # accents, parens and spaces
        text = "\xe7afe au lait (boisson)"
        assert lyrics.slug(text) == "cafe-au-lait-boisson"
        text = "Multiple  spaces -- and symbols! -- merged"
        assert lyrics.slug(text) == "multiple-spaces-and-symbols-merged"
        text = "\u200bno-width-space"
        assert lyrics.slug(text) == "no-width-space"

        # variations of dashes should get standardized
        dashes = ["\u200d", "\u2010"]
        for dash1, dash2 in itertools.combinations(dashes, 2):
            assert lyrics.slug(dash1) == lyrics.slug(dash2)
