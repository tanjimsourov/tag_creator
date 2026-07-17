from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.robotparser
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from ..config import Settings
from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs
from .base import ProviderClient

LOGGER = logging.getLogger(__name__)

# Per-host robots.txt cache shared across worker threads: host -> (parser|None, expiry).
# A None parser means "could not fetch robots" and is treated as disallow, but the
# decision is cached so a slow/broken host is only contacted once per TTL.
_ROBOTS_CACHE: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_TTL_SECONDS = 3600.0

FIELD_LABELS = {
    "title": ["title", "song", "track", "track title"],
    "artist": ["artist", "artists", "performer", "primary artist"],
    "album": ["album", "release", "single"],
    "album_artist": ["album artist", "release artist"],
    "year": ["year"],
    "date": ["release date", "released", "date", "published"],
    "genre": ["genre", "genres"],
    "subgenre": ["subgenre", "sub-genre", "style", "styles"],
    "bpm": ["bpm", "tempo", "beats per minute"],
    "key": ["key", "song key", "musical key", "camelot"],
    "language": ["language", "lyrics language", "vocal language"],
    "mood": ["mood", "primary mood"],
    "moods": ["moods", "mood tags"],
    "energy": ["energy", "intensity"],
    "danceability": ["danceability", "dance"],
    "valence": ["valence", "happiness", "positiveness"],
    "label": ["label", "record label", "labels"],
    "catalog_number": ["catalog number", "catalog no", "cat no", "cat#", "cat. no."],
    "composer": ["composer", "composers", "written by", "writer", "songwriter", "songwriters"],
    "publisher": ["publisher", "published by", "publishing"],
    "copyright": ["copyright", "phonographic copyright", "copyright holder"],
    "isrc": ["isrc", "isrc code"],
    "instruments": ["instruments", "instrumentation"],
    "vocals": ["vocals", "vocal"],
}

LABEL_TO_FIELD = {
    label.lower(): field
    for field, labels in FIELD_LABELS.items()
    for label in labels
}


class WebDiscoveryClient(ProviderClient):
    provider_name = "web_discovery"

    def __init__(self, store, rate_limiter, settings: Settings) -> None:
        super().__init__(store, rate_limiter)
        self.enabled = settings.web_scraping_enabled
        self.max_results = settings.web_max_results
        self.allowed_domains = [domain.lower() for domain in settings.web_allowed_domains]
        self.search_endpoint = settings.web_search_endpoint
        self.max_fetches = settings.web_max_fetches_per_run  # 0 = unlimited
        self.fetches = 0
        self._fetch_lock = threading.Lock()
        self.session.headers.update({"User-Agent": "SMC-Tag-Creator/0.1 metadata discovery"})

    def _reserve_fetch(self) -> bool:
        """Reserve one page fetch against the per-run budget (thread-safe)."""
        if self.max_fetches <= 0:
            return True
        with self._fetch_lock:
            if self.fetches >= self.max_fetches:
                return False
            self.fetches += 1
            return True

    def is_configured(self) -> bool:
        return self.enabled and bool(self.allowed_domains)

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(host == domain or host.endswith("." + domain) for domain in self.allowed_domains)

    def _robots_parser(self, scheme: str, netloc: str) -> urllib.robotparser.RobotFileParser | None:
        key = f"{scheme}://{netloc}"
        now = time.time()
        with _ROBOTS_LOCK:
            entry = _ROBOTS_CACHE.get(key)
            if entry and entry[1] > now:
                return entry[0]

        parser: urllib.robotparser.RobotFileParser | None = None
        robots_url = f"{key}/robots.txt"
        try:
            response = self.session.get(robots_url, timeout=(5, 10))
            if response.ok:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
            elif 400 <= response.status_code < 500:
                # No/forbidden robots.txt -> permissive (RFC behaviour: allow all).
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
        except Exception as exc:  # noqa: BLE001 - never let robots fetch stall the run
            LOGGER.debug("robots fetch failed for %s: %s", robots_url, exc)
            parser = None

        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[key] = (parser, now + _ROBOTS_TTL_SECONDS)
        return parser

    def _robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        parser = self._robots_parser(parsed.scheme, parsed.netloc)
        if parser is None:
            return False
        try:
            return parser.can_fetch(self.session.headers.get("User-Agent", "*"), url)
        except Exception:  # noqa: BLE001
            return False

    def _search_urls(self, query: str) -> list[str]:
        # DuckDuckGo HTML scraping is best-effort: any failure degrades to "no web
        # results" and the pipeline simply relies on the other providers.
        self.rate_limiter.wait(self.provider_name)
        try:
            response = self.session.get(self.search_endpoint, params={"q": query}, timeout=(10, 30))
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("web search failed: %s", exc)
            return []
        if not response.ok:
            return []
        try:
            soup = BeautifulSoup(response.text, "html.parser")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("web search parse failed: %s", exc)
            return []
        urls: list[str] = []
        for link in soup.select("a.result__a, a[href]"):
            href = link.get("href", "")
            if "uddg=" in href:
                parsed = urlparse(href)
                href = unquote(parse_qs(parsed.query).get("uddg", [""])[0])
            if href.startswith("http") and self._allowed(href):
                urls.append(href)
            if len(urls) >= self.max_results:
                break
        return urls

    def _search_many_urls(self, queries: list[str]) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        per_query_limit = self.max_results
        for query in queries:
            for url in self._search_urls(query):
                key = url.split("#", 1)[0]
                if key in seen:
                    continue
                seen.add(key)
                urls.append(url)
                if len(urls) >= per_query_limit:
                    return urls
        return urls

    @staticmethod
    def _extract_json_ld(soup: BeautifulSoup) -> list[dict]:
        blocks: list[dict] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.get_text(strip=True))
            except Exception:
                continue
            if isinstance(data, list):
                blocks.extend(item for item in data if isinstance(item, dict))
            elif isinstance(data, dict):
                blocks.append(data)
        return blocks

    @staticmethod
    def _find_text_field(text: str, labels: list[str]) -> str:
        for label in labels:
            patterns = [
                rf"\b{re.escape(label)}\b\s*(?:[:\-–—]|is|=)\s*([A-Za-z0-9 #+/.,&'%()\-]{{1,120}})",
                rf"\b{re.escape(label)}\b\s+([A-Za-z0-9 #+/.,&'%()\-]{{1,120}})",
            ]
            for pattern in patterns:
                match = re.search(pattern, text, flags=re.I)
                if match:
                    value = unescape(match.group(1)).strip(" .,\n\t")
                    value = re.split(
                        r"\s{2,}|(?:\s+\|\s+)|(?:\s+[A-Z][A-Za-z ]{2,24}:)|(?:\s+(?:BPM|Key|Genre|Album|Artist|Label)\b)",
                        value,
                        maxsplit=1,
                    )[0]
                    return value.strip(" .,\n\t")
        return ""

    @staticmethod
    def _clean_scalar(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(item) for item in value if item)
        if isinstance(value, dict):
            return str(value.get("name", "") or "")
        return str(value).strip()

    @staticmethod
    def _meta_content(soup: BeautifulSoup, names: list[str]) -> str:
        for name in names:
            selector = (
                f'meta[name="{name}"], meta[property="{name}"], '
                f'meta[name="{name.lower()}"], meta[property="{name.lower()}"]'
            )
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                return str(tag.get("content", "")).strip()
        return ""

    @staticmethod
    def _normalize_label(text: str) -> str:
        text = unescape(text)
        text = re.sub(r"[\s:#*]+$", "", text.strip().lower())
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _clean_value(text: str) -> str:
        text = unescape(text or "")
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n:;|-")
        text = re.sub(r"\b(?:view|more|edit|share|copy|lyrics)\b.*$", "", text, flags=re.I).strip(" \t\r\n:;|-")
        return text[:180].strip()

    def _put_labeled_value(self, fields: dict[str, str], label: str, value: str) -> None:
        normalized = self._normalize_label(label)
        field = LABEL_TO_FIELD.get(normalized)
        if not field:
            for known_label, known_field in LABEL_TO_FIELD.items():
                if normalized == known_label or normalized.endswith(" " + known_label):
                    field = known_field
                    break
        if not field:
            return
        self._put(fields, field, self._clean_value(value))

    def _extract_pair_fields(self, soup: BeautifulSoup) -> dict[str, str]:
        fields: dict[str, str] = {}
        for row in soup.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                self._put_labeled_value(fields, cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True))
        for dt in soup.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                self._put_labeled_value(fields, dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
        for element in soup.select("[itemprop]"):
            prop = str(element.get("itemprop", "")).strip().lower()
            value = element.get("content") or element.get("datetime") or element.get_text(" ", strip=True)
            prop_map = {
                "name": "title",
                "byartist": "artist",
                "inalbum": "album",
                "genre": "genre",
                "datepublished": "date",
                "datecreated": "date",
                "isrccode": "isrc",
                "publisher": "publisher",
                "copyrightnotice": "copyright",
            }
            if prop in prop_map:
                self._put(fields, prop_map[prop], value)
        return fields

    @staticmethod
    def _normalize_bpm(value: str) -> str:
        match = re.search(r"\b([4-9]\d|1\d{2}|2[0-4]\d)\b", value)
        return match.group(1) if match else ""

    @staticmethod
    def _normalize_key(value: str) -> str:
        match = re.search(r"\b([A-G](?:#|b)?\s*(?:maj(?:or)?|min(?:or)?|m)?)\b", value, flags=re.I)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip()

    @staticmethod
    def _normalize_ratio(value: str) -> str:
        percent = re.search(r"\b(100|[1-9]?\d)(?:\.\d+)?\s*%", value)
        if percent:
            return str(round(float(percent.group(1)) / 100, 3))
        decimal = re.search(r"\b(?:0?\.\d+|1\.0+)\b", value)
        if decimal:
            number = max(0.0, min(1.0, float(decimal.group(0))))
            return str(round(number, 3))
        low = value.lower()
        if any(word in low for word in ("high", "energetic", "strong")):
            return "0.75"
        if any(word in low for word in ("low", "calm", "soft")):
            return "0.35"
        if "medium" in low or "moderate" in low:
            return "0.55"
        return ""

    @staticmethod
    def _normalize_year(value: str) -> str:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", value)
        return match.group(1) if match else ""

    @staticmethod
    def _normalize_isrc(value: str) -> str:
        match = re.search(r"\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b", value.upper().replace("-", ""))
        return match.group(1) if match else ""

    @staticmethod
    def _put(fields: dict[str, str], key: str, value: object) -> None:
        text = WebDiscoveryClient._clean_scalar(value)
        if text and not fields.get(key):
            fields[key] = text

    def _extract_fields(self, html: str, url: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        json_ld = self._extract_json_ld(soup)
        fields: dict[str, str] = {}
        fields.update(self._extract_pair_fields(soup))
        for block in json_ld:
            self._put(fields, "title", block.get("name", ""))
            by_artist = block.get("byArtist") or block.get("creator")
            if isinstance(by_artist, dict):
                self._put(fields, "artist", by_artist.get("name", ""))
            elif isinstance(by_artist, str):
                self._put(fields, "artist", by_artist)
            album = block.get("inAlbum") or block.get("album")
            self._put(fields, "album", album)
            self._put(fields, "genre", block.get("genre", ""))
            self._put(fields, "date", block.get("datePublished", "") or block.get("dateCreated", ""))
            self._put(fields, "isrc", block.get("isrcCode", "") or block.get("isrc", ""))
            self._put(fields, "publisher", block.get("publisher", ""))
            self._put(fields, "copyright", block.get("copyrightNotice", "") or block.get("copyrightHolder", ""))
        meta_fields = {
            "title": self._meta_content(soup, ["og:title", "twitter:title", "title"]),
            "artist": self._meta_content(soup, ["music:musician", "artist"]),
            "album": self._meta_content(soup, ["music:album", "album"]),
            "genre": self._meta_content(soup, ["music:genre", "genre"]),
            "date": self._meta_content(soup, ["music:release_date", "release_date", "date"]),
            "isrc": self._meta_content(soup, ["music:isrc", "isrc"]),
        }
        for key, value in meta_fields.items():
            self._put(fields, key, value)
        text = soup.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        discovered = {field: self._find_text_field(text, labels) for field, labels in FIELD_LABELS.items()}
        if discovered["bpm"]:
            discovered["bpm"] = self._normalize_bpm(discovered["bpm"])
        if discovered["key"]:
            discovered["key"] = self._normalize_key(discovered["key"])
        if discovered["isrc"]:
            discovered["isrc"] = self._normalize_isrc(discovered["isrc"])
        if discovered["date"]:
            year = self._normalize_year(discovered["date"])
            if year and not fields.get("year"):
                fields["year"] = year
        for feature in ("danceability", "valence"):
            if discovered[feature]:
                discovered[feature] = self._normalize_ratio(discovered[feature])
        if discovered["energy"]:
            ratio = self._normalize_ratio(discovered["energy"])
            if ratio:
                number = float(ratio)
                discovered["energy"] = "high" if number >= 0.67 else ("low" if number <= 0.40 else "medium")
        for key, value in discovered.items():
            self._put(fields, key, value)
        if fields.get("isrc"):
            normalized_isrc = self._normalize_isrc(fields["isrc"])
            if normalized_isrc:
                fields["isrc"] = normalized_isrc
        if fields.get("date") and not fields.get("year"):
            year = self._normalize_year(fields["date"])
            if year:
                fields["year"] = year
        if fields:
            fields["analysis_summary"] = f"Public web metadata discovered from {urlparse(url).netloc}"
        return {key: value for key, value in fields.items() if value}

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        candidates = candidate_track_pairs(media, limit=3)
        if not self.is_configured() or not candidates:
            return None
        queries: list[str] = []
        for artist, title in candidates:
            track_query = " ".join(item for item in [artist, title] if item).strip()
            if not track_query:
                continue
            queries.extend(
                [
                    f'"{track_query}" artist title album year genre',
                    f'"{track_query}" release date album label catalog number',
                    f'"{track_query}" isrc composer publisher copyright',
                    f'"{track_query}" genre bpm key mood',
                    f'"{track_query}" tempo key danceability energy',
                    f'"{track_query}" musicstax bpm key energy danceability valence',
                    f'"{track_query}" tunebat bpm key camelot popularity happiness energy',
                    f'"{track_query}" songdata bpm key energy danceability valence',
                    f'"{track_query}" chosic genre mood bpm key',
                    f'"{track_query}" language instruments vocals',
                    f'"{track_query}" "record label" "ISRC"',
                    f'"{track_query}" "catalog number" Discogs MusicBrainz',
                    f'"{track_query}" songwriter composer credits',
                    f'site:musicbrainz.org "{track_query}" recording',
                    f'site:discogs.com "{track_query}" release',
                    f'site:last.fm "{track_query}"',
                    f'site:deezer.com "{track_query}"',
                    f'site:genius.com "{track_query}"',
                    f'site:allmusic.com "{track_query}"',
                    f'site:rateyourmusic.com "{track_query}"',
                    f'site:officialcharts.com "{track_query}"',
                    f'site:theaudiodb.com "{track_query}"',
                    f'site:tunebat.com "{track_query}"',
                    f'site:musicstax.com "{track_query}"',
                    f'site:songbpm.com "{track_query}"',
                    f'site:getsongbpm.com "{track_query}"',
                    f'site:songdata.io "{track_query}"',
                    f'site:chosic.com "{track_query}"',
                ]
            )
        urls = self._search_many_urls(queries)
        combined: dict[str, str] = {}
        used_urls: list[str] = []
        for url in urls:
            if not self._robots_allowed(url):
                continue
            if not self._reserve_fetch():
                LOGGER.info("web_discovery per-run fetch cap (%s) reached; skipping further fetches", self.max_fetches)
                break
            self.rate_limiter.wait(self.provider_name)
            try:
                response = self.session.get(url, timeout=(10, 30))
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("web page fetch failed for %s: %s", url, exc)
                continue
            if not response.ok or "text/html" not in response.headers.get("content-type", ""):
                continue
            fields = self._extract_fields(response.text, url)
            if fields.get("title") or fields.get("artist"):
                plausible = any(
                    plausible_track_match(
                        title,
                        artist,
                        fields.get("title", title),
                        fields.get("artist", artist),
                        min_title=0.45,
                        min_artist=0.35,
                    )[0]
                    for artist, title in candidates
                )
                if not plausible:
                    continue
            if fields:
                combined.update({key: value for key, value in fields.items() if key not in combined})
                used_urls.append(url)
            if len(used_urls) >= 4:
                break
        if not combined:
            return ProviderResult("web_discovery", 0, {}, notes="no allowed web metadata found")
        return ProviderResult(
            "web_discovery",
            0.62,
            combined,
            source_url=used_urls[0],
            raw={"urls": used_urls},
            notes="allowlisted public metadata extraction; no lyrics scraping",
        )
