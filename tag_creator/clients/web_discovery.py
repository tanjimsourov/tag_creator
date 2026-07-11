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
from .base import ProviderClient

LOGGER = logging.getLogger(__name__)

# Per-host robots.txt cache shared across worker threads: host -> (parser|None, expiry).
# A None parser means "could not fetch robots" and is treated as disallow, but the
# decision is cached so a slow/broken host is only contacted once per TTL.
_ROBOTS_CACHE: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_TTL_SECONDS = 3600.0


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
            pattern = rf"{re.escape(label)}\s*[:\-]\s*([A-Za-z0-9 #+/.,&'-]{{1,80}})"
            match = re.search(pattern, text, flags=re.I)
            if match:
                return unescape(match.group(1)).strip(" .,\n\t")
        return ""

    def _extract_fields(self, html: str, url: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        json_ld = self._extract_json_ld(soup)
        fields: dict[str, str] = {}
        for block in json_ld:
            fields.setdefault("title", str(block.get("name", "") or ""))
            by_artist = block.get("byArtist") or block.get("creator")
            if isinstance(by_artist, dict):
                fields.setdefault("artist", str(by_artist.get("name", "") or ""))
            elif isinstance(by_artist, str):
                fields.setdefault("artist", by_artist)
            fields.setdefault("genre", str(block.get("genre", "") or ""))
            fields.setdefault("date", str(block.get("datePublished", "") or block.get("dateCreated", "") or ""))
        text = soup.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        discovered = {
            "genre": self._find_text_field(text, ["genre", "genres"]),
            "subgenre": self._find_text_field(text, ["subgenre", "sub-genre", "style"]),
            "bpm": self._find_text_field(text, ["bpm", "tempo"]),
            "key": self._find_text_field(text, ["key"]),
            "mood": self._find_text_field(text, ["mood", "moods"]),
            "energy": self._find_text_field(text, ["energy"]),
            "danceability": self._find_text_field(text, ["danceability"]),
        }
        for key, value in discovered.items():
            if value and key not in fields:
                fields[key] = value
        if fields:
            fields["analysis_summary"] = f"Public web metadata discovered from {urlparse(url).netloc}"
        return {key: value for key, value in fields.items() if value}

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        if not self.is_configured() or not title:
            return None
        query = f'{artist} {title} genre bpm key mood'
        urls = self._search_urls(query)
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
                plausible, _, _ = plausible_track_match(
                    title,
                    artist,
                    fields.get("title", title),
                    fields.get("artist", artist),
                    min_title=0.45,
                    min_artist=0.35,
                )
                if not plausible:
                    continue
            if fields:
                combined.update({key: value for key, value in fields.items() if key not in combined})
                used_urls.append(url)
            if len(used_urls) >= 2:
                break
        if not combined:
            return ProviderResult("web_discovery", 0, {}, notes="no allowed web metadata found")
        return ProviderResult(
            "web_discovery",
            0.58,
            combined,
            source_url=used_urls[0],
            raw={"urls": used_urls},
            notes="allowlisted public metadata extraction; no lyrics scraping",
        )
