from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.robotparser
from collections import defaultdict
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

CATALOG_FACT_FIELDS = {
    "title", "artist", "album", "album_artist", "year", "date", "genre",
    "subgenre", "language", "label", "catalog_number", "composer", "publisher",
    "copyright", "isrc", "track_number", "disc_number",
}
AUDIO_ANALYSIS_FIELDS = {
    "bpm", "key", "mood", "moods", "energy", "danceability", "valence",
    "instruments", "vocals",
}
CATALOG_DOMAINS = {
    "musicbrainz.org", "discogs.com", "allmusic.com", "theaudiodb.com",
    "deezer.com", "last.fm", "genius.com", "wikidata.org", "wikipedia.org",
}
ANALYSIS_DOMAINS = {
    "tunebat.com", "songbpm.com", "musicstax.com", "songdata.io",
    "getsongbpm.com", "getsongkey.com", "chosic.com",
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
        self.max_queries_per_file = settings.web_max_queries_per_file
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
            response = self.session.get(robots_url, timeout=(3, 5))
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
            response = self.session.get(self.search_endpoint, params={"q": query}, timeout=(5, 15))
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
        for query in queries[: self.max_queries_per_file]:
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
    def _domain(url: str) -> str:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
        return host[4:] if host.startswith("www.") else host

    @staticmethod
    def _value_key(field: str, value: str) -> str:
        text = unescape(value).strip().lower()
        if field in {"isrc", "catalog_number"}:
            return re.sub(r"[^a-z0-9]", "", text)
        if field in {"bpm", "year", "track_number", "disc_number", "danceability", "valence"}:
            return text
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    @staticmethod
    def _field_domain_weight(field: str, domain: str) -> float:
        if field in CATALOG_FACT_FIELDS:
            return 1.0 if domain in CATALOG_DOMAINS else 0.55
        if field in AUDIO_ANALYSIS_FIELDS:
            return 1.0 if domain in ANALYSIS_DOMAINS else 0.65
        return 0.70

    @staticmethod
    def _identity_match(fields: dict[str, str], candidates: list[tuple[str, str]]) -> tuple[bool, float]:
        page_title = fields.get("title", "").strip()
        page_artist = fields.get("artist", "").strip()
        # A page that does not name both track and artist is not sufficient
        # evidence for importing arbitrary table/body values.
        if not page_title or not page_artist:
            return False, 0.0
        scores: list[float] = []
        for artist, title in candidates:
            plausible, title_score, artist_score = plausible_track_match(
                title,
                artist,
                page_title,
                page_artist,
                min_title=0.62,
                min_artist=0.55,
            )
            if plausible:
                scores.append((title_score * 0.60) + (artist_score * 0.40))
        return (bool(scores), max(scores) if scores else 0.0)

    @staticmethod
    def _queries(artist: str, title: str) -> list[str]:
        identity = f'"{artist}" "{title}"' if artist else f'"{title}"'
        return [
            (
                f"{identity} album release date label ISRC catalog number "
                "(site:musicbrainz.org OR site:discogs.com OR site:theaudiodb.com)"
            ),
            (
                f"{identity} composer publisher genre language "
                "(site:allmusic.com OR site:genius.com OR site:last.fm)"
            ),
            (
                f"{identity} BPM key danceability energy valence "
                "(site:tunebat.com OR site:musicstax.com OR site:songbpm.com OR site:getsongkey.com)"
            ),
            f'{identity} "track" "artist" "album" "release date" "genre"',
        ]

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
            queries.extend(self._queries(artist, title))
        urls = self._search_many_urls(queries)
        evidence: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
        used_urls: list[str] = []
        for url in urls:
            if not self._robots_allowed(url):
                continue
            if not self._reserve_fetch():
                LOGGER.info("web_discovery per-run fetch cap (%s) reached; skipping further fetches", self.max_fetches)
                break
            self.rate_limiter.wait(self.provider_name)
            try:
                response = self.session.get(url, timeout=(5, 15))
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("web page fetch failed for %s: %s", url, exc)
                continue
            if not response.ok or "text/html" not in response.headers.get("content-type", ""):
                continue
            fields = self._extract_fields(response.text, url)
            plausible, identity_score = self._identity_match(fields, candidates)
            if not plausible:
                continue
            if fields:
                used_urls.append(url)
                domain = self._domain(url)
                for field, value in fields.items():
                    if not value or field in {"analysis_summary", "analysis_json"}:
                        continue
                    key = self._value_key(field, value)
                    if not key:
                        continue
                    item = evidence[field].setdefault(
                        key,
                        {"value": value, "score": 0.0, "domains": set(), "urls": []},
                    )
                    domains = item["domains"]
                    if domain not in domains:
                        item["score"] = float(item["score"]) + (
                            self._field_domain_weight(field, domain) * identity_score
                        )
                        domains.add(domain)
                    item["urls"].append(url)
            if len(used_urls) >= 4:
                break

        combined: dict[str, str] = {}
        field_evidence: dict[str, dict[str, object]] = {}
        for field, candidates_by_value in evidence.items():
            best = max(
                candidates_by_value.values(),
                key=lambda item: (float(item["score"]), len(item["domains"])),
            )
            combined[field] = str(best["value"])
            field_evidence[field] = {
                "score": round(float(best["score"]), 3),
                "domains": sorted(best["domains"]),
                "urls": list(dict.fromkeys(best["urls"])),
            }
        if not combined:
            return ProviderResult("web_discovery", 0, {}, notes="no allowed web metadata found")
        return ProviderResult(
            "web_discovery",
            0.62,
            combined,
            source_url=used_urls[0],
            raw={"urls": used_urls, "field_evidence": field_evidence},
            notes="identity-verified allowlisted public metadata with field-level source consensus; no lyrics scraping",
        )
