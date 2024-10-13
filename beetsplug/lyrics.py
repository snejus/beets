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

import atexit
import errno
import itertools
import math
import os.path
import re
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import cached_property, partial, total_ordering
from html import unescape
from http import HTTPStatus
from typing import TYPE_CHECKING, ClassVar, Iterable, Iterator, NamedTuple
from urllib.parse import quote

import requests
from unidecode import unidecode

import beets
from beets import plugins, ui
from beets.autotag.hooks import string_dist

if TYPE_CHECKING:
    from beets.importer import ImportTask
    from beets.library import Item

    from ._typing import GeniusAPI, GoogleCustomSearchAPI, LRCLibAPI

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

USER_AGENT = f"beets/{beets.__version__}"
INSTRUMENTAL_LYRICS = "[Instrumental]"

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


class NotFoundError(requests.exceptions.HTTPError):
    pass


class TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        """Wrap the request method to raise an exception on HTTP errors."""
        kwargs.setdefault("timeout", 10)
        r = super().request(*args, **kwargs)
        if r.status_code == HTTPStatus.NOT_FOUND:
            raise NotFoundError("HTTP Error: Not Found", response=r)
        r.raise_for_status()

        return r


r_session = TimeoutSession()
r_session.headers.update({"User-Agent": USER_AGENT})


@atexit.register
def close_session():
    """Close the requests session on shut down."""
    r_session.close()


# Utilities.


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

    def generate_alternatives(string, patterns):
        """Generate string alternatives by extracting first matching group for
        each given pattern.
        """
        alternatives = [string]
        for pattern in patterns:
            match = re.search(pattern, string, re.IGNORECASE)
            if match:
                alternatives.append(match.group(1))
        return alternatives

    title, artist, artist_sort = (
        item.title.strip(),
        item.artist.strip(),
        item.artist_sort.strip(),
    )
    if not title or not artist:
        return ()

    patterns = [
        # Remove any featuring artists from the artists name
        rf"(.*?) {plugins.feat_tokens()}"
    ]
    artists = generate_alternatives(artist, patterns)
    # Use the artist_sort as fallback only if it differs from artist to avoid
    # repeated remote requests with the same search terms
    if artist_sort and artist.lower() != artist_sort.lower():
        artists.append(artist_sort)

    patterns = [
        # Remove a parenthesized suffix from a title string. Common
        # examples include (live), (remix), and (acoustic).
        r"(.+?)\s+[(].*[)]$",
        # Remove any featuring artists from the title
        r"(.*?) {}".format(plugins.feat_tokens(for_artist=False)),
        # Remove part of title after colon ':' for songs with subtitles
        r"(.+?)\s*:.*",
    ]
    titles = generate_alternatives(title, patterns)

    # Check for a dual song (e.g. Pink Floyd - Speak to Me / Breathe)
    # and each of them.
    multi_titles = []
    for title in titles:
        multi_titles.append([title])
        if "/" in title:
            multi_titles.append([x.strip() for x in title.split("/")])

    return itertools.product(artists, multi_titles)


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

    def debug(self, message: str, *args) -> None:
        """Log a debug message with the class name."""
        self._log.debug(f"{self.__class__.__name__}: {message}", *args)

    def info(self, message: str, *args) -> None:
        """Log an info message with the class name."""
        self._log.info(f"{self.__class__.__name__}: {message}", *args)

    def warn(self, message: str, *args) -> None:
        """Log warning with the class name."""
        self._log.warning(f"{self.__class__.__name__}: {message}", *args)

    def fetch_text(self, url: str, **kwargs) -> str:
        """Return text / HTML data from the given URL.

        Set the encoding to None to let requests handle it because some sites
        set it incorrectly.
        """
        self.debug("Fetching HTML from {}", url)
        r = r_session.get(url, **kwargs)
        r.encoding = None
        return r.text

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


class Backend(RequestHandler):
    REQUIRES_BS = False

    def __init__(self, config, log):
        self._log = log
        self.config = config

    def fetch(
        self, artist: str, title: str, album: str, length: int
    ) -> str | None:
        raise NotImplementedError


@dataclass
@total_ordering
class LRCLyrics:
    #: Percentage tolerance for max duration difference between lyrics and item.
    DURATION_DIFF_TOLERANCE = 0.05

    target_duration: float
    duration: float
    instrumental: bool
    plain: str
    synced: str | None

    def __le__(self, other: LRCLyrics) -> bool:
        """Compare two lyrics items by their score."""
        return self.dist < other.dist

    @classmethod
    def make(
        cls, candidate: LRCLibAPI.Item, target_duration: float
    ) -> LRCLyrics:
        return cls(
            target_duration,
            candidate["duration"] or 0.0,
            candidate["instrumental"],
            candidate["plainLyrics"],
            candidate["syncedLyrics"],
        )

    @cached_property
    def duration_dist(self) -> float:
        """Return the absolute difference between lyrics and target duration."""
        return abs(self.duration - self.target_duration)

    @cached_property
    def is_valid(self) -> bool:
        """Return whether the lyrics item is valid.
        Lyrics duration must be within the tolerance defined by
        :attr:`DURATION_DIFF_TOLERANCE`.
        """
        return (
            self.duration_dist
            <= self.target_duration * self.DURATION_DIFF_TOLERANCE
        )

    @cached_property
    def dist(self) -> tuple[float, bool]:
        """Distance/score of the given lyrics item.

        Return a tuple with the following values:
        1. Absolute difference between lyrics and target duration
        2. Boolean telling whether synced lyrics are available.

        Best lyrics match is the one that has the closest duration to
        ``target_duration`` and has synced lyrics available.
        """
        return self.duration_dist, not self.synced

    def get_text(self, want_synced: bool) -> str:
        if self.instrumental:
            return INSTRUMENTAL_LYRICS

        if want_synced and self.synced:
            return "\n".join(map(str.strip, self.synced.splitlines()))

        return self.plain


class LRCLib(Backend):
    """Fetch lyrics from the LRCLib API."""

    BASE_URL = "https://lrclib.net/api"
    GET_URL = f"{BASE_URL}/get"
    SEARCH_URL = f"{BASE_URL}/search"

    def fetch_candidates(
        self, artist: str, title: str, album: str, length: int
    ) -> Iterator[list[LRCLibAPI.Item]]:
        """Yield lyrics candidates for the given song data.

        Firstly, attempt to GET lyrics directly, and then search the API if
        lyrics are not found or the duration does not match.

        Return an iterator over lists of candidates.
        """
        base_params = {"artist_name": artist, "track_name": title}
        get_params = {**base_params, "duration": length}
        if album:
            get_params["album_name"] = album

        with suppress(NotFoundError):
            yield [self.fetch_json(self.GET_URL, params=get_params)]

        yield self.fetch_json(self.SEARCH_URL, params=base_params)

    @classmethod
    def pick_best_match(cls, lyrics: Iterable[LRCLyrics]) -> LRCLyrics | None:
        """Return best matching lyrics item from the given list."""
        return min((li for li in lyrics if li.is_valid), default=None)

    def fetch(
        self, artist: str, title: str, album: str, length: int
    ) -> str | None:
        """Fetch lyrics text for the given song data."""
        fetch = partial(self.fetch_candidates, artist, title, album, length)
        make = partial(LRCLyrics.make, target_duration=length)
        pick = self.pick_best_match
        try:
            return next(
                filter(None, map(pick, (map(make, x) for x in fetch())))
            ).get_text(self.config["synced"])
        except StopIteration:
            return None


class DirectBackend(Backend):
    """A backend for fetching lyrics directly."""

    URL_TEMPLATE: ClassVar[str]  #: May include formatting placeholders

    @classmethod
    def encode(cls, text: str) -> str:
        """Encode the string for inclusion in a URL."""
        raise NotImplementedError

    @classmethod
    def build_url(cls, *args: str) -> str:
        return cls.URL_TEMPLATE.format(*map(cls.encode, args))


class MusiXmatch(DirectBackend):
    URL_TEMPLATE = "https://www.musixmatch.com/lyrics/{}/{}"

    REPLACEMENTS = {
        r"\s+": "-",
        "<": "Less_Than",
        ">": "Greater_Than",
        "#": "Number_",
        r"[\[\{]": "(",
        r"[\]\}]": ")",
    }

    @classmethod
    def encode(cls, text: str) -> str:
        for old, new in cls.REPLACEMENTS.items():
            text = re.sub(old, new, text)

        return quote(unidecode(text))

    def fetch(self, artist: str, title: str, *_) -> str | None:
        url = self.build_url(artist, title)

        html = self.fetch_text(url)
        if "We detected that your IP is blocked" in html:
            self.warn("Failed: Blocked IP address")
            return None
        html_parts = html.split('<p class="mxm-lyrics__content')
        # Sometimes lyrics come in 2 or more parts
        lyrics_parts = []
        for html_part in html_parts:
            lyrics_parts.append(re.sub(r"^[^>]+>|</p>.*", "", html_part))
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


class Html:
    collapse_space = partial(re.compile(r"(^| ) +", re.M).sub, r"\1")
    expand_br = partial(re.compile(r"\s*<br[^>]*>\s*", re.I).sub, "\n")
    #: two newlines between paragraphs on the same line (musica, letras.mus.br)
    merge_blocks = partial(re.compile(r"(?<!>)</p><p[^>]*>").sub, "\n\n")
    #: a single new line between paragraphs on separate lines
    #: (paroles.net, sweetslyrics.com, lacoccinelle.net)
    merge_lines = partial(re.compile(r"</p>\s+<p[^>]*>(?!___)").sub, "\n")
    #: remove empty divs (lacoccinelle.net)
    remove_empty_divs = partial(re.compile(r"<div[^>]*>\s*</div>").sub, "")
    #: remove Google Ads tags (musica.com)
    remove_aside = partial(re.compile("<aside .+?</aside>").sub, "")
    #: remove adslot-Content_1 div from the lyrics text (paroles.net)
    remove_adslot = partial(
        re.compile(r"\n</div>[^\n]+-- Content_\d+ --.*?\n<div>", re.S).sub,
        "\n",
    )
    #: remove text formatting (azlyrics.com, lacocinelle.net)
    remove_formatting = partial(
        re.compile(r" *</?(i|em|pre|strong)[^>]*>").sub, ""
    )

    @classmethod
    def normalize_space(cls, text: str) -> str:
        text = unescape(text).replace("\r", "").replace("\xa0", " ")
        return cls.collapse_space(cls.expand_br(text))

    @classmethod
    def remove_ads(cls, text: str) -> str:
        return cls.remove_adslot(cls.remove_aside(text))

    @classmethod
    def merge_paragraphs(cls, text: str) -> str:
        return cls.merge_blocks(cls.merge_lines(cls.remove_empty_divs(text)))


class SoupMixin:
    @classmethod
    def pre_process_html(cls, html: str) -> str:
        """Pre-process the HTML content before scraping."""
        return Html.normalize_space(html)

    @classmethod
    def get_soup(cls, html: str) -> BeautifulSoup:
        return BeautifulSoup(cls.pre_process_html(html), "html.parser")


class SearchResult(NamedTuple):
    artist: str
    title: str
    url: str


class SearchBackend(SoupMixin, Backend):
    REQUIRES_BS = True

    @cached_property
    def dist_thresh(self) -> float:
        return self.config["dist_thresh"].get(float)

    def check_match(
        self, target_artist: str, target_title: str, result: SearchResult
    ) -> bool:
        """Check if the given search result is a 'good enough' match."""
        max_dist = max(
            string_dist(target_artist, result.artist),
            string_dist(target_title, result.title),
        )

        if (max_dist := round(max_dist, 2)) <= self.dist_thresh:
            return True

        if math.isclose(max_dist, self.dist_thresh, abs_tol=0.4):
            # log out the candidate that did not make it but was close.
            # This may show a matching candidate with some noise in the name
            self.debug(
                "({}, {}) does not match ({}, {}) but dist was close: {:.2f}",
                result.artist,
                result.title,
                target_artist,
                target_title,
                max_dist,
            )

        return False

    def search(self, artist: str, title: str) -> Iterable[SearchResult]:
        """Search for the given query and yield search results."""
        raise NotImplementedError

    def get_results(self, artist: str, title: str) -> Iterable[SearchResult]:
        check_match = partial(self.check_match, artist, title)
        for candidate in self.search(artist, title):
            if check_match(candidate):
                yield candidate

    def fetch(self, artist: str, title: str, *_) -> str | None:
        """Fetch lyrics for the given artist and title."""
        for result in self.get_results(artist, title):
            if (html := self.fetch_text(result.url)) and (
                lyrics := self.scrape(html)
            ):
                return lyrics

        return None

    @classmethod
    def scrape(cls, html: str) -> str | None:
        """Scrape the lyrics from the given HTML."""
        raise NotImplementedError


class Genius(SearchBackend):
    """Fetch lyrics from Genius via genius-api.

    Because genius doesn't allow accessing lyrics via the api, we first query
    the api for a url matching our artist & title, then scrape the HTML text
    for the JSON data containing the lyrics.

    Adapted from
    bigishdata.com/2016/09/27/getting-song-lyrics-from-geniuss-api-scraping
    """

    SEARCH_URL = "https://api.genius.com/search"
    LYRICS_IN_JSON_RE = re.compile(r'(?<=.\\"html\\":\\").*?(?=(?<!\\)\\")')
    remove_backslash = partial(re.compile(r"\\(?=[^\\])").sub, "")

    @cached_property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f'Bearer {self.config["genius_api_key"]}'}

    def search(self, artist: str, title: str) -> Iterable[SearchResult]:
        search_data: GeniusAPI.Search = self.fetch_json(
            self.SEARCH_URL,
            params={"q": f"{artist} {title}"},
            headers=self.headers,
        )
        for r in (hit["result"] for hit in search_data["response"]["hits"]):
            yield SearchResult(r["artist_names"], r["title"], r["url"])

    @classmethod
    def scrape(cls, html: str) -> str | None:
        if m := cls.LYRICS_IN_JSON_RE.search(html):
            html_text = cls.remove_backslash(m[0]).replace(r"\n", "\n")
            return cls.get_soup(html_text).get_text().strip()

        return None


class Tekstowo(SoupMixin, DirectBackend):
    """Fetch lyrics from Tekstowo.pl."""

    REQUIRES_BS = True
    URL_TEMPLATE = "https://www.tekstowo.pl/piosenka,{},{}.html"

    non_alpha_to_underscore = partial(re.compile(r"\W").sub, "_")

    @classmethod
    def encode(cls, text: str) -> str:
        return cls.non_alpha_to_underscore(unidecode(text.lower()))

    def fetch(self, artist: str, title: str, *_) -> str | None:
        # We are expecting to receive a 404 since we are guessing the URL.
        # Thus suppress the error so that it does not end up in the logs.
        with suppress(NotFoundError):
            return self.scrape(self.fetch_text(self.build_url(artist, title)))

        return None

    @classmethod
    def scrape(cls, html: str) -> str | None:
        soup = cls.get_soup(html)

        if lyrics_div := soup.select_one("div.song-text > div.inner-text"):
            return lyrics_div.get_text()

        return None


class Google(SearchBackend):
    """Fetch lyrics from Google search results."""

    SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

    #: Exclude some letras.mus.br pages which do not contain lyrics.
    EXCLUDE_PAGES = [
        "significado.html",
        "traduccion.html",
        "traducao.html",
        "significados.html",
    ]

    #: Regular expression to match noise in the URL title.
    URL_TITLE_NOISE_RE = re.compile(
        r"""
\b
(
      paroles(\ et\ traduction|\ de\ chanson)?
    | letras?(\ de)?
    | liedtexte
    | original\ song\ full\ text\.
    | official
    | 20[12]\d\ version
    | (absolute\ |az)?lyrics(\ complete)?
    | www\S+
    | \S+\.(com|net|mus\.br)
)
([^\w.]|$)
""",
        re.IGNORECASE | re.VERBOSE,
    )
    #: Split cleaned up URL title into artist and title parts.
    URL_TITLE_PARTS_RE = re.compile(r" +(?:[ :|-]+|par|by) +")

    @classmethod
    def pre_process_html(cls, html: str) -> str:
        """Pre-process the HTML content before scraping."""
        html = Html.remove_ads(super().pre_process_html(html))
        return Html.remove_formatting(Html.merge_paragraphs(html))

    def fetch_text(self, *args, **kwargs) -> str:
        """Handle an error so that we can continue with the next URL."""
        with self.handle_request():
            return super().fetch_text(*args, **kwargs)

    @staticmethod
    def get_part_dist(artist: str, title: str, part: str) -> float:
        """Return the distance between the given part and the artist and title.

        A number between -1 and 1 is returned, where -1 means the part is
        closer to the artist and 1 means it is closer to the title.
        """
        return string_dist(artist, part) - string_dist(title, part)

    @classmethod
    def make_search_result(
        cls, artist: str, title: str, item: GoogleCustomSearchAPI.Item
    ) -> SearchResult:
        """Parse artist and title from the URL title and return a search result."""
        url_title = (
            # get full title from metatags if available
            item.get("pagemap", {}).get("metatags", [{}])[0].get("og:title")
            # default to the dispolay title
            or item["title"]
        )
        clean_title = cls.URL_TITLE_NOISE_RE.sub("", url_title).strip(" .-|")
        # split it into parts which may be part of the artist or the title
        # `dict.fromkeys` removes duplicates keeping the order
        parts = list(dict.fromkeys(cls.URL_TITLE_PARTS_RE.split(clean_title)))

        if len(parts) == 1:
            part = parts[0]
            if m := re.search(rf"(?i)\W*({re.escape(title)})\W*", part):
                # artist and title may not have a separator
                result_title = m[1]
                result_artist = part.replace(m[0], "")
            else:
                # assume that this is the title
                result_artist, result_title = "", parts[0]
        else:
            # sort parts by their similarity to the artist
            parts.sort(key=lambda p: cls.get_part_dist(artist, title, p))
            result_artist, result_title = parts[0], " ".join(parts[1:])

        return SearchResult(result_artist, result_title, item["link"])

    def search(self, artist: str, title: str) -> Iterable[SearchResult]:
        params = {
            "key": self.config["google_API_key"].as_str(),
            "cx": self.config["google_engine_ID"].as_str(),
            "q": f"{artist} {title}",
            "siteSearch": "www.musixmatch.com",
            "siteSearchFilter": "e",
            "excludeTerms": ", ".join(self.EXCLUDE_PAGES),
        }

        data: GoogleCustomSearchAPI.Response = self.fetch_json(
            self.SEARCH_URL, params=params
        )
        for item in data.get("items", []):
            yield self.make_search_result(artist, title, item)

    @classmethod
    def scrape(cls, html: str) -> str | None:
        # Get the longest text element (if any).
        if strings := sorted(cls.get_soup(html).stripped_strings, key=len):
            return strings[-1]

        return None


class LyricsPlugin(RequestHandler, plugins.BeetsPlugin):
    SOURCES = ["lrclib", "google", "musixmatch", "genius", "tekstowo"]
    SOURCE_BACKENDS = {
        "google": Google,
        "musixmatch": MusiXmatch,
        "genius": Genius,
        "tekstowo": Tekstowo,
        "lrclib": LRCLib,
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
                "dist_thresh": 0.11,
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
                "sources": [s for s in self.SOURCES if s != "musixmatch"],
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

        available_sources = list(self.SOURCES)
        sources = plugins.sanitize_choices(
            self.config["sources"].as_str_seq(), available_sources
        )

        if not HAS_BEAUTIFUL_SOUP:
            sources = self.sanitize_bs_sources(sources)

        if "google" in sources:
            if not self.config["google_API_key"].get():
                # We log a *debug* message here because the default
                # configuration includes `google`. This way, the source
                # is silent by default but can be enabled just by
                # setting an API key.
                self.debug("Disabling google source: " "no API key configured.")
                sources.remove("google")

        self.config["bing_lang_from"] = [
            x.lower() for x in self.config["bing_lang_from"].as_str_seq()
        ]

        if not HAS_LANGDETECT and self.config["bing_client_secret"].get():
            self.warn(
                "To use bing translations, you need to install the langdetect "
                "module. See the documentation for further details."
            )

        self.backends = [
            self.SOURCE_BACKENDS[s](self.config, self._log.getChild(s))
            for s in sources
        ]

    def sanitize_bs_sources(self, sources):
        enabled_sources = []
        for source in sources:
            if self.SOURCE_BACKENDS[source].REQUIRES_BS:
                self._log.debug(
                    "To use the %s lyrics source, you must "
                    "install the beautifulsoup4 module. See "
                    "the documentation for further details." % source
                )
            else:
                enabled_sources.append(source)

        return enabled_sources

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
                        item, write, opts.force_refetch or self.config["force"]
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

    def imported(self, _, task: ImportTask) -> None:
        """Import hook for fetching lyrics automatically."""
        if self.config["auto"]:
            for item in task.imported_items():
                self.fetch_item_lyrics(item, False, self.config["force"])

    def fetch_item_lyrics(self, item: Item, write: bool, force: bool) -> None:
        """Fetch and store lyrics for a single item. If ``write``, then the
        lyrics will also be written to the file itself.
        """
        # Skip if the item already has lyrics.
        if not force and item.lyrics:
            self.info("🔵 Lyrics already present: {}", item)
            return

        lyrics_matches = []
        album, length = item.album, round(item.length)
        for artist, titles in search_pairs(item):
            lyrics_matches = [
                self.get_lyrics(artist, title, album, length)
                for title in titles
            ]
            if any(lyrics_matches):
                break

        lyrics = "\n\n---\n\n".join(filter(None, lyrics_matches))

        if lyrics:
            self.info("🟢 Found lyrics: {0}", item)
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
            self.info("🔴 Lyrics not found: {}", item)
            lyrics = self.config["fallback"].get()

        if lyrics not in {None, item.lyrics}:
            item.lyrics = lyrics
            if write:
                item.try_write()
            item.store()

    def get_lyrics(self, artist: str, title: str, *args) -> str | None:
        """Fetch lyrics, trying each source in turn. Return a string or
        None if no lyrics were found.
        """
        self.info("Fetching lyrics for {} - {}", artist, title)
        for backend in self.backends:
            with backend.handle_request():
                if lyrics := backend.fetch(artist, title, *args):
                    return lyrics

        return None

    def append_translation(self, text, to_lang):
        from xml.etree import ElementTree

        if not (token := self.bing_access_token):
            self.warn(
                "Could not get Bing Translate API access token. "
                "Check your 'bing_client_secret' password."
            )
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
