# This file is part of beets.
# Copyright 2016, Adrian Sampson.
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

"""Fetches, embeds, and displays lyrics."""

from __future__ import annotations

import ast
import atexit
import difflib
import errno
import itertools
import json
import os.path
import re
import urllib
from contextlib import contextmanager
from functools import cached_property, lru_cache, partial
from html import unescape
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlparse

import requests
from unidecode import unidecode

import beets
from beets import plugins, ui
from beets.autotag.hooks import string_dist
from beets.util import FT_TOKEN_RE, split_ft_artist

if TYPE_CHECKING:
    from .types import GeniusSearchResult, JSONDict, LRCLibItem

try:
    from bs4 import BeautifulSoup

    HAS_BEAUTIFUL_SOUP = True
except ImportError:
    HAS_BEAUTIFUL_SOUP = False

try:
    import langdetect

    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False

BREAK_RE = re.compile(r"\n?\s*<br([\s|/][^>]*)*>\s*\n?", re.I)
COLON_PART_RE = re.compile(r"\s*:.*")
remove_parens = partial(re.compile(r"\s+[(].*[)]$").sub, "")

USER_AGENT = f"beets/{beets.__version__}"

# The content for the base index.rst generated in ReST mode.
REST_INDEX_TEMPLATE = """Lyrics
======

* :ref:`Song index <genindex>`
* :ref:`search`

Artist index:

.. toctree::
   :maxdepth: 1
   :glob:

   artists/*
"""

# The content for the base conf.py generated.
REST_CONF_TEMPLATE = """# -*- coding: utf-8 -*-
master_doc = 'index'
project = 'Lyrics'
copyright = 'none'
author = 'Various Authors'
latex_documents = [
    (master_doc, 'Lyrics.tex', project,
     author, 'manual'),
]
epub_title = project
epub_author = author
epub_publisher = author
epub_copyright = copyright
epub_exclude_files = ['search.html']
epub_tocdepth = 1
epub_tocdup = False
"""


class TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        """Wrap the request method to raise an exception on HTTP errors."""
        kwargs.setdefault("timeout", 10)
        r = super().request(*args, **kwargs)
        r.raise_for_status()

        return r


r_session = TimeoutSession()
r_session.headers.update({"User-Agent": USER_AGENT})


@atexit.register
def close_session():
    """Close the requests session on shut down."""
    r_session.close()


# Utilities.


def extract_text_between(html, start_marker, end_marker):
    try:
        _, html = html.split(start_marker, 1)
        html, _ = html.split(end_marker, 1)
    except ValueError:
        return ""
    return html


def search_pairs(item):
    """Yield a pairs of artists and titles to search for.

    The first item in the pair is the name of the artist, the second
    item is a list of song names.

    In addition to the artist and title obtained from the `item` the
    method tries to strip extra information like paranthesized suffixes
    and featured artists from the strings and add them as candidates.
    The artist sort name is added as a fallback candidate to help in
    cases where artist name includes special characters or is in a
    non-latin script.
    The method also tries to split multiple titles separated with `/`.
    """

    title, artist, artist_sort = item.title, item.artist, item.artist_sort

    artists = {artist, split_ft_artist(artist)[0]}
    if artist.lower() != artist_sort.lower():
        artists.add(artist_sort)

    titles = {
        remove_parens(title),
        FT_TOKEN_RE.split(title)[0],
        COLON_PART_RE.sub("", title),
    }

    # Check for a dual song (e.g. Pink Floyd - Speak to Me / Breathe)
    # and each of them.
    multi_titles = []
    for title in titles:
        multi_titles.append([title])
        if "/" in title:
            multi_titles.append([x.strip() for x in title.split("/")])

    return itertools.product(
        sorted(artists, key=lambda a: a != artist), multi_titles
    )


def slug(text: str) -> str:
    """Make a URL-safe, human-readable version of the given text

    This will do the following:

    1. decode unicode characters into ASCII
    2. shift everything to lowercase
    3. strip whitespace
    4. replace other non-word characters with dashes
    5. strip extra dashes
    """
    return re.sub(r"\W+", "-", unidecode(text).lower().strip()).strip("-")


class RequestHandler:
    _log: beets.logging.Logger

    def debug(self, message: str, *args: Any) -> None:
        """Log a debug message with the class name."""
        self._log.debug(f"{self.__class__.__name__}: {message}", *args)

    def info(self, message: str, *args: Any) -> None:
        """Log an info message with the class name."""
        self._log.info(f"{self.__class__.__name__}: {message}", *args)

    def warn(self, message: str, *args: Any) -> None:
        """Log warning with the class name."""
        self._log.warning(f"{self.__class__.__name__}: {message}", *args)

    def fetch_text(self, url: str, **kwargs) -> str:
        """Return text / HTML data from the given URL."""
        self.debug("Fetching HTML from {}", url)
        return r_session.get(url, **kwargs).text

    def fetch_json(self, url: str, **kwargs):
        """Return JSON data from the given URL."""
        self.debug("Fetching JSON from {}", url)
        return r_session.get(url, **kwargs).json()

    @contextmanager
    def handle_request(self) -> Iterator[None]:
        try:
            yield
        except requests.JSONDecodeError:
            self.warn("Could not decode response JSON data")
        except requests.RequestException as exc:
            self.warn("Request error: {}", exc)


class BackendType(type):
    """Metaclass for the :class:`Backend` class.

    It keeps track of defined subclasses and provides access to them through
    the base class:

    >>> Backend["genius"]  # beetsplug.lyrics.Genius
    >>> list(Backend)  # ["lrclib", "musixmatch", "genius", "tekstowo", "google"]
    """

    _registry: dict[str, BackendType] = {}
    REQUIRES_BS: bool

    def __new__(cls, name: str, bases: tuple[type, ...], attrs) -> BackendType:
        """Create a new instance of the class and add it to the registry."""
        new_class = super().__new__(cls, name, bases, attrs)
        if bases:
            cls._registry[name.lower()] = new_class
        return new_class

    @classmethod
    def __getitem__(cls, key: str) -> BackendType:
        return cls._registry[key]

    @classmethod
    def __iter__(cls) -> Iterator[str]:
        return iter(cls._registry)


class Backend(RequestHandler, metaclass=BackendType):
    REQUIRES_BS = False

    def __init__(self, config, log):
        self._log = log
        self.config = config

    def fetch(self, artist, title, album=None, length=None):
        raise NotImplementedError


class LRCLib(Backend):
    base_url = "https://lrclib.net/api/search"

    @staticmethod
    def get_rank(
        target_duration: float, litem: LRCLibItem
    ) -> tuple[float, bool]:
        """Rank the given lyrics item.

        Return a tuple with the following values:
        1. Difference between item lyrics duration and the item duration in sec
        2. Boolean telling whether synced lyrics are available.
        """
        return (
            abs(litem["duration"] - target_duration),
            not litem["syncedLyrics"],
        )

    @classmethod
    def pick_lyrics(
        cls, target_duration: float, data: list[LRCLibItem]
    ) -> LRCLibItem:
        """Return best matching lyrics item from the given list.

        Note that the incoming list is guaranteed to be non-empty.
        """
        return min(data, key=lambda item: cls.get_rank(target_duration, item))

    def fetch(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        length: float = 0.0,
    ) -> str | None:
        """Fetch lyrics for the given artist, title, and album."""
        params = {
            "artist_name": artist,
            "track_name": title,
            "album_name": album,
        }

        data: list[LRCLibItem]
        if data := self.fetch_json(self.base_url, params=params):
            item = self.pick_lyrics(length, data)

            if self.config["synced"] and (synced := item["syncedLyrics"]):
                return synced

            return item["plainLyrics"]

        return None


class MusiXmatch(Backend):
    REPLACEMENTS = {
        r"\s+": "-",
        "<": "Less_Than",
        ">": "Greater_Than",
        "#": "Number_",
        r"[\[\{]": "(",
        r"[\]\}]": ")",
    }

    URL_PATTERN = "https://www.musixmatch.com/lyrics/{}/{}"

    @classmethod
    def _encode(cls, s: str) -> str:
        s = unidecode(s)
        for old, new in cls.REPLACEMENTS.items():
            s = re.sub(old, new, s)

        return urllib.parse.quote(s)

    def build_url(self, artist, title):
        return self.URL_PATTERN.format(
            self._encode(artist.title()),
            self._encode(title.title()),
        )

    def fetch(self, artist, title, album=None, length=None):
        url = self.build_url(artist, title)

        html = self.fetch_text(url)
        if "We detected that your IP is blocked" in html:
            self.warn("Failed: Blocked IP address")
            return None
        html_parts = html.split('<p class="mxm-lyrics__content')
        # Sometimes lyrics come in 2 or more parts
        lyrics_parts = []
        for html_part in html_parts:
            lyrics_parts.append(extract_text_between(html_part, ">", "</p>"))
        lyrics = "\n".join(lyrics_parts)
        lyrics = lyrics.strip(',"').replace("\\n", "\n")
        # another odd case: sometimes only that string remains, for
        # missing songs. this seems to happen after being blocked
        # above, when filling in the CAPTCHA.
        if "Instant lyrics for all your music." in lyrics:
            return None
        # sometimes there are non-existent lyrics with some content
        if "Lyrics | Musixmatch" in lyrics:
            return None
        return lyrics


class SimilarityMixin:
    config: beets.IncludeLazyConfig

    @cached_property
    def dist_thresh(self) -> float:
        return self.config["dist_thresh"].get(float)

    def check_match(
        self, target_artist: str, target_title: str, artist: str, title: str
    ) -> bool:
        """Check if the given artist and title are 'good enough' match."""
        return (
            max(
                string_dist(target_artist, artist),
                string_dist(target_title, title),
            )
            < self.dist_thresh
        )


class Genius(SimilarityMixin, Backend):
    """Fetch lyrics from Genius via genius-api.

    Simply adapted from
    bigishdata.com/2016/09/27/getting-song-lyrics-from-geniuss-api-scraping/
    """

    REQUIRES_BS = True
    JSON_BLOCK_RE = re.compile(r"(?<=JSON.parse\(').*?(?='\);\n)")

    base_url = "https://api.genius.com"
    search_url = f"{base_url}/search"

    def __init__(self, config, log):
        super().__init__(config, log)
        self.api_key = config["genius_api_key"].as_str()
        self.headers = {"Authorization": f"Bearer {self.api_key}"}

    def fetch(self, artist, title, album=None, length=None):
        """Fetch lyrics from genius.com

        Because genius doesn't allow accessing lyrics via the api,
        we first query the api for a url matching our artist & title,
        then attempt to scrape that url for the lyrics.
        """

        if (
            data := self.fetch_json(
                self.search_url,
                params={"q": f"{artist} {title}".lower()},
                headers=self.headers,
            )
        ) and (url := self.find_lyrics_url(data, artist, title)):
            return self.scrape_lyrics(self.fetch_text(url))

    def find_lyrics_url(
        self, data: JSONDict, artist: str, title: str
    ) -> str | None:
        """Find URL to the lyrics page the given artist and title.

        https://docs.genius.com/#search-h2.
        """
        check = partial(self.check_match, artist, title)
        for item in data["response"]["hits"]:
            result: GeniusSearchResult = item["result"]
            if check(result["artist_names"], REMOVE_PARENS(result["title"])):
                return result["url"]

        return None

    @classmethod
    @lru_cache
    def scrape_lyrics(cls, html: str) -> str | None:
        for m in cls.JSON_BLOCK_RE.finditer(html):
            if "songPage" in (text := m.group()):
                data = json.loads(
                    ast.literal_eval(f"'{text}'").replace(r"\$", "$")
                )
                html = data["songPage"]["lyricsData"]["body"]["html"]
                return get_soup(html).get_text()

        return None


class Tekstowo(Backend):
    # Fetch lyrics from Tekstowo.pl.
    REQUIRES_BS = True

    BASE_URL = "https://www.tekstowo.pl"
    URL_PATTERN = BASE_URL + "/szukaj,{}.html"

    def build_url(self, artist, title):
        artistitle = f"{artist.title()} {title.title()}"
        return self.URL_PATTERN.format(
            urllib.parse.quote_plus(unidecode(artistitle))
        )

    def fetch(self, artist, title, album=None, length=None):
        search_results = self.fetch_text(self.build_url(title, artist))
        song_page_url = self.parse_search_results(search_results)
        if not song_page_url:
            return None

        song_page_html = self.fetch_text(song_page_url)
        return self.scrape_lyrics(song_page_html, artist, title)

    def parse_search_results(self, html: str) -> str | None:
        soup = get_soup(html)

        content_div = soup.find("div", class_="content")
        if not content_div:
            return None

        card_div = content_div.find("div", class_="card")
        if not card_div:
            return None

        song_rows = card_div.find_all("div", class_="box-przeboje")
        if not song_rows:
            return None

        song_row = song_rows[0]
        if not song_row:
            return None

        link = song_row.find("a")
        if not link:
            return None

        return self.BASE_URL + link.get("href")

    def scrape_lyrics(self, html: str, artist: str, title: str) -> str | None:
        soup = get_soup(html)

        info_div = soup.find("div", class_="col-auto")
        if not info_div:
            return None

        info_elements = info_div.find_all("a")
        if not info_elements:
            return None

        html_title = info_elements[-1].get_text()
        html_artist = info_elements[-2].get_text()

        title_dist = string_dist(html_title, title)
        artist_dist = string_dist(html_artist, artist)

        thresh = self.config["dist_thresh"].get(float)
        if title_dist > thresh or artist_dist > thresh:
            return None

        lyrics_div = soup.select("div.song-text > div.inner-text")
        if not lyrics_div:
            return None

        return lyrics_div[0].get_text()


def remove_credits(text):
    """Remove first/last line of text if it contains the word 'lyrics'
    eg 'Lyrics by songsdatabase.com'
    """
    textlines = text.split("\n")
    credits = None
    for i in (0, -1):
        if textlines and "lyrics" in textlines[i].lower():
            credits = textlines.pop(i)
    if credits:
        text = "\n".join(textlines)
    return text


def _scrape_strip_cruft(html: str) -> str:
    """Clean up HTML"""
    html = unescape(html)

    html = html.replace("\r", "\n")  # Normalize EOL.
    html = re.sub(r" +", " ", html)  # Whitespaces collapse.
    html = BREAK_RE.sub("\n", html)  # <br> eats up surrounding '\n'.
    html = re.sub(r"(?s)<(script).*?</\1>", "", html)  # Strip script tags.
    html = re.sub("\u2005", " ", html)  # replace unicode with regular space
    html = re.sub("<aside .+?</aside>", "", html)  # remove Google Ads tags
    html = re.sub(r"</?(em|strong)[^>]*>", "", html)  # remove bold / italics

    html = "\n".join([x.strip() for x in html.strip().split("\n")])
    return re.sub(r"\n{3,}", r"\n\n", html)


def _scrape_merge_paragraphs(html):
    html = re.sub(r"</p>\s*<p(\s*[^>]*)>", "\n", html)
    return re.sub(r"<div .*>\s*</div>", "\n", html)


def get_soup(html: str) -> BeautifulSoup:
    html = _scrape_strip_cruft(html)
    html = _scrape_merge_paragraphs(html)

    return BeautifulSoup(html, "html.parser")


class Google(Backend):
    """Fetch lyrics from Google search results."""

    REQUIRES_BS = True

    def __init__(self, config, log):
        super().__init__(config, log)
        self.api_key = config["google_API_key"].as_str()
        self.engine_id = config["google_engine_ID"].as_str()

    @staticmethod
    def scrape_lyrics(html: str) -> str | None:
        soup = get_soup(html)

        # Get the longest text element (if any).
        strings = sorted(soup.stripped_strings, key=len, reverse=True)
        if strings:
            return strings[0]
        return None

    def is_lyrics(self, text, artist=None):
        """Determine whether the text seems to be valid lyrics."""
        if not text:
            return False
        bad_triggers_occ = []
        nb_lines = text.count("\n")
        if nb_lines <= 1:
            self.debug("Ignoring too short lyrics '{}'", text)
            return False
        elif nb_lines < 5:
            bad_triggers_occ.append("too_short")
        else:
            # Lyrics look legit, remove credits to avoid being penalized
            # further down
            text = remove_credits(text)

        bad_triggers = ["lyrics", "copyright", "property", "links"]
        if artist:
            bad_triggers += [artist]

        for item in bad_triggers:
            bad_triggers_occ += [item] * len(
                re.findall(r"\W%s\W" % item, text, re.I)
            )

        if bad_triggers_occ:
            self.debug("Bad triggers detected: {}", bad_triggers_occ)
        return len(bad_triggers_occ) < 2

    BY_TRANS = ["by", "par", "de", "von"]
    LYRICS_TRANS = ["lyrics", "paroles", "letras", "liedtexte"]

    def is_page_candidate(self, url_link, url_title, title, artist):
        """Return True if the URL title makes it a good candidate to be a
        page that contains lyrics of title by artist.
        """
        title = slug(title)
        artist = re.escape(slug(artist))
        sitename = urlparse(url_link).netloc
        url_title = slug(url_title)

        # Check if URL title contains song title (exact match)
        if url_title.find(title) != -1:
            return True

        # or try extracting song title from URL title and check if
        # they are close enough
        tokens = (
            [by + "-" + artist for by in self.BY_TRANS]
            + [artist, sitename, sitename.replace("www.", "")]
            + self.LYRICS_TRANS
        )
        song_title = re.sub("(%s)" % "|".join(tokens), "", url_title)

        song_title = song_title.strip("-|")
        typo_ratio = 0.9
        ratio = difflib.SequenceMatcher(None, song_title, title).ratio()
        return ratio >= typo_ratio

    def fetch(self, artist, title, album=None, length=None):
        query = f"{artist} {title}"
        url = "https://www.googleapis.com/customsearch/v1?key=%s&cx=%s&q=%s" % (
            self.api_key,
            self.engine_id,
            urllib.parse.quote(query.encode("utf-8")),
        )

        data = self.fetch_json(url)
        if "error" in data:
            reason = data["error"]["errors"][0]["reason"]
            self.debug("Error: {}", reason)
            return None

        if "items" in data.keys():
            for item in data["items"]:
                url_link = item["link"]
                url_title = item.get("title", "")
                if not self.is_page_candidate(
                    url_link, url_title, title, artist
                ):
                    continue
                lyrics = self.scrape_lyrics(self.fetch_text(url_link))
                if not lyrics:
                    continue

                if self.is_lyrics(lyrics, artist):
                    self.debug("Got lyrics from {}", item["displayLink"])
                    return lyrics

        return None


class LyricsPlugin(RequestHandler, plugins.BeetsPlugin):
    @cached_property
    def backends(self) -> dict[str, Backend]:
        user_sources = self.config["sources"].get()
        chosen = plugins.sanitize_choices(user_sources, Backend)

        disabled = set()
        if not HAS_BEAUTIFUL_SOUP:
            disabled |= {n for n in chosen if Backend[n].REQUIRES_BS}
            if disabled:
                self.debug(
                    "Disabling {} sources: missing beautifulsoup4 module",
                    disabled,
                )

        elif "google" in chosen and not self.config["google_API_key"].get():
            self.debug("Disabling Google source: no API key configured.")
            disabled.add("google")

        return {
            s: Backend[s](self.config, self._log.getChild(s))
            for s in chosen
            if s not in disabled
        }

    def __init__(self):
        super().__init__()
        self.import_stages = [self.imported]
        self.config.add(
            {
                "auto": True,
                "bing_client_secret": None,
                "bing_lang_from": [],
                "bing_lang_to": None,
                "google_API_key": None,
                "google_engine_ID": "009217259823014548361:lndtuqkycfu",
                "genius_api_key": (
                    "Ryq93pUGm8bM6eUWwD_M3NOFFDAtp2yEE7W"
                    "76V-uFL5jks5dNvcGCdarqFjDhP9c"
                ),
                "fallback": None,
                "force": False,
                "local": False,
                "synced": False,
                # Musixmatch is disabled by default as they are currently blocking
                # requests with the beets user agent.
                "sources": [s for s in Backend if s != "musixmatch"],
                "dist_thresh": 0.1,
            }
        )
        self.config["bing_client_secret"].redact = True
        self.config["google_API_key"].redact = True
        self.config["google_engine_ID"].redact = True
        self.config["genius_api_key"].redact = True

        # State information for the ReST writer.
        # First, the current artist we're writing.
        self.artist = "Unknown artist"
        # The current album: False means no album yet.
        self.album = False
        # The current rest file content. None means the file is not
        # open yet.
        self.rest = None

        self.config["bing_lang_from"] = [
            x.lower() for x in self.config["bing_lang_from"].as_str_seq()
        ]

        if not HAS_LANGDETECT and self.config["bing_client_secret"].get():
            self.warn(
                "To use bing translations, you need to install the langdetect "
                "module. See the documentation for further details."
            )

    @cached_property
    def bing_access_token(self) -> str | None:
        params = {
            "client_id": "beets",
            "client_secret": self.config["bing_client_secret"],
            "scope": "https://api.microsofttranslator.com",
            "grant_type": "client_credentials",
        }

        oauth_url = "https://datamarket.accesscontrol.windows.net/v2/OAuth2-13"
        with self.handle_request():
            r = r_session.post(oauth_url, params=params)
            return r.json()["access_token"]

        self.warn(
            "Could not get Bing Translate API access token. "
            "Check your 'bing_client_secret' password."
        )
        return None

    def commands(self):
        cmd = ui.Subcommand("lyrics", help="fetch song lyrics")
        cmd.parser.add_option(
            "-p",
            "--print",
            dest="printlyr",
            action="store_true",
            default=False,
            help="print lyrics to console",
        )
        cmd.parser.add_option(
            "-r",
            "--write-rest",
            dest="writerest",
            action="store",
            default=None,
            metavar="dir",
            help="write lyrics to given directory as ReST files",
        )
        cmd.parser.add_option(
            "-f",
            "--force",
            dest="force_refetch",
            action="store_true",
            default=False,
            help="always re-download lyrics",
        )
        cmd.parser.add_option(
            "-l",
            "--local",
            dest="local_only",
            action="store_true",
            default=False,
            help="do not fetch missing lyrics",
        )

        def func(lib, opts, args):
            # The "write to files" option corresponds to the
            # import_write config value.
            write = ui.should_write()
            if opts.writerest:
                self.writerest_indexes(opts.writerest)
            items = lib.items(ui.decargs(args))
            for item in items:
                if not opts.local_only and not self.config["local"]:
                    self.fetch_item_lyrics(
                        lib,
                        item,
                        write,
                        opts.force_refetch or self.config["force"],
                    )
                if item.lyrics:
                    if opts.printlyr:
                        ui.print_(item.lyrics)
                    if opts.writerest:
                        self.appendrest(opts.writerest, item)
            if opts.writerest and items:
                # flush last artist & write to ReST
                self.writerest(opts.writerest)
                ui.print_("ReST files generated. to build, use one of:")
                ui.print_(
                    "  sphinx-build -b html %s _build/html" % opts.writerest
                )
                ui.print_(
                    "  sphinx-build -b epub %s _build/epub" % opts.writerest
                )
                ui.print_(
                    (
                        "  sphinx-build -b latex %s _build/latex "
                        "&& make -C _build/latex all-pdf"
                    )
                    % opts.writerest
                )

        cmd.func = func
        return [cmd]

    def appendrest(self, directory, item):
        """Append the item to an ReST file

        This will keep state (in the `rest` variable) in order to avoid
        writing continuously to the same files.
        """

        if slug(self.artist) != slug(item.albumartist):
            # Write current file and start a new one ~ item.albumartist
            self.writerest(directory)
            self.artist = item.albumartist.strip()
            self.rest = "%s\n%s\n\n.. contents::\n   :local:\n\n" % (
                self.artist,
                "=" * len(self.artist),
            )

        if self.album != item.album:
            tmpalbum = self.album = item.album.strip()
            if self.album == "":
                tmpalbum = "Unknown album"
            self.rest += "{}\n{}\n\n".format(tmpalbum, "-" * len(tmpalbum))
        title_str = ":index:`%s`" % item.title.strip()
        block = "| " + item.lyrics.replace("\n", "\n| ")
        self.rest += "{}\n{}\n\n{}\n\n".format(
            title_str, "~" * len(title_str), block
        )

    def writerest(self, directory):
        """Write self.rest to a ReST file"""
        if self.rest is not None and self.artist is not None:
            path = os.path.join(
                directory, "artists", slug(self.artist) + ".rst"
            )
            with open(path, "wb") as output:
                output.write(self.rest.encode("utf-8"))

    def writerest_indexes(self, directory):
        """Write conf.py and index.rst files necessary for Sphinx

        We write minimal configurations that are necessary for Sphinx
        to operate. We do not overwrite existing files so that
        customizations are respected."""
        try:
            os.makedirs(os.path.join(directory, "artists"))
        except OSError as e:
            if e.errno == errno.EEXIST:
                pass
            else:
                raise
        indexfile = os.path.join(directory, "index.rst")
        if not os.path.exists(indexfile):
            with open(indexfile, "w") as output:
                output.write(REST_INDEX_TEMPLATE)
        conffile = os.path.join(directory, "conf.py")
        if not os.path.exists(conffile):
            with open(conffile, "w") as output:
                output.write(REST_CONF_TEMPLATE)

    def imported(self, session, task):
        """Import hook for fetching lyrics automatically."""
        if self.config["auto"]:
            for item in task.imported_items():
                self.fetch_item_lyrics(
                    session.lib, item, False, self.config["force"]
                )

    def fetch_item_lyrics(self, lib, item, write, force):
        """Fetch and store lyrics for a single item. If ``write``, then the
        lyrics will also be written to the file itself.
        """
        # Skip if the item already has lyrics.
        if not force and item.lyrics:
            self.info("Lyrics already present: {}", item)
            return

        lyrics = None
        album = item.album
        length = round(item.length)
        for artist, titles in search_pairs(item):
            lyrics = [
                self.get_lyrics(artist, title, album=album, length=length)
                for title in titles
            ]
            if any(lyrics):
                break

        lyrics = "\n\n---\n\n".join(filter(None, lyrics))

        if lyrics:
            self.info("Lyrics found: {}", item)
            if HAS_LANGDETECT and self.config["bing_client_secret"].get():
                lang_from = langdetect.detect(lyrics)
                if self.config["bing_lang_to"].get() != lang_from and (
                    not self.config["bing_lang_from"]
                    or (lang_from in self.config["bing_lang_from"].as_str_seq())
                ):
                    lyrics = self.append_translation(
                        lyrics, self.config["bing_lang_to"]
                    )
        else:
            self.info("Lyrics not found: {}", item)
            fallback = self.config["fallback"].get()
            if fallback:
                lyrics = fallback
            else:
                return
        item.lyrics = lyrics
        if write:
            item.try_write()
        item.store()

    def get_lyrics(self, artist, title, album=None, length=None):
        """Fetch lyrics, trying each source in turn. Return a string or
        None if no lyrics were found.
        """
        self.debug("Fetching lyrics for {} - {}", artist, title)
        for backend in self.backends.values():
            with backend.handle_request():
                if lyrics := backend.fetch(
                    artist, title, album=album, length=length
                ):
                    return lyrics

    def append_translation(self, text, to_lang):
        from xml.etree import ElementTree

        if not (token := self.bing_access_token):
            return text

        # Extract unique lines to limit API request size per song
        lines = text.split("\n")
        unique_lines = set(lines)
        url = "https://api.microsofttranslator.com/v2/Http.svc/Translate"
        with self.handle_request():
            text = self.fetch_text(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"text": "|".join(unique_lines), "to": to_lang},
            )
            if translated := ElementTree.fromstring(text.encode("utf-8")).text:
                # Use a translation mapping dict to build resulting lyrics
                translations = dict(zip(unique_lines, translated.split("|")))
                return "".join(f"{ln} / {translations[ln]}\n" for ln in lines)

        return text
