from __future__ import annotations

import logging
import re
from collections import defaultdict
from html import unescape

from bs4 import BeautifulSoup

from ..config import Settings
from ..matching import normalize_text, similarity
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs, clean_track_text
from .base import ProviderClient

LOGGER = logging.getLogger(__name__)

ARTIST_NOISE_WORDS = {
    "album",
    "artist",
    "audio",
    "cover",
    "genre",
    "karaoke",
    "lyrics",
    "music",
    "official",
    "song",
    "track",
    "video",
    "youtube",
}


class ArtistSearchClient(ProviderClient):
    """Resolve missing/wrong artists from search-result snippets.

    This is intentionally narrow: it only emits an ``artist`` field when the
    search result text names the requested title and the candidate artist is
    clearly separated by common music-result patterns.
    """

    provider_name = "artist_search"
    connect_timeout = 5.0
    read_timeout = 12.0

    def __init__(self, store, rate_limiter, settings: Settings) -> None:
        super().__init__(store, rate_limiter)
        self.enabled = settings.artist_search_enabled
        self.search_endpoint = settings.artist_search_endpoint
        self.max_results = settings.artist_search_max_results
        self.min_confidence = settings.artist_search_min_confidence
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def is_configured(self) -> bool:
        return self.enabled and bool(self.search_endpoint)

    @staticmethod
    def _title_candidates(media: MediaFile) -> list[str]:
        titles: list[str] = []
        seen: set[str] = set()
        for _artist, title in candidate_track_pairs(media, limit=5):
            cleaned = clean_track_text(title)
            if not cleaned:
                continue
            key = normalize_text(cleaned)
            if key and key not in seen:
                seen.add(key)
                titles.append(cleaned)
        return titles

    @staticmethod
    def _result_blocks(html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        blocks: list[str] = []
        for selector in ("div.g", "[data-sokoban-container]", ".result", "a", "div"):
            for element in soup.select(selector):
                text = re.sub(r"\s+", " ", element.get_text(" ", strip=True))
                if text and len(text) >= 8:
                    blocks.append(unescape(text))
                if len(blocks) >= 80:
                    return blocks
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        return [unescape(text)] if text else []

    @staticmethod
    def _clean_artist(value: str, title: str = "") -> str:
        cleaned = unescape(value or "")
        cleaned = re.split(
            r"\s+(?:-|–|—|\||•|·|:)\s+(?:YouTube|Spotify|Apple Music|Genius|Last\.fm|KaraFun|lyrics?|official)\b",
            cleaned,
            maxsplit=1,
            flags=re.I,
        )[0]
        cleaned = re.split(
            r"\s+(?:on|from|by)\s+(?:YouTube|Spotify|Apple Music|Genius|Last\.fm|KaraFun)\b",
            cleaned,
            maxsplit=1,
            flags=re.I,
        )[0]
        cleaned = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:-_|")
        title_key = normalize_text(title)
        artist_key = normalize_text(cleaned)
        if not cleaned or len(cleaned) > 80:
            return ""
        if title_key and (artist_key == title_key or artist_key in title_key or title_key in artist_key):
            return ""
        if any(noise == artist_key or f" {noise} " in f" {artist_key} " for noise in ARTIST_NOISE_WORDS):
            return ""
        if re.search(r"https?://|www\.|\.com\b", cleaned, flags=re.I):
            return ""
        return cleaned

    @classmethod
    def _artists_from_block(cls, block: str, title: str) -> list[tuple[str, float, str]]:
        candidates: list[tuple[str, float, str]] = []
        title_pattern = re.escape(title)
        normalized_block = normalize_text(block)
        normalized_title = normalize_text(title)
        if not normalized_title or normalized_title not in normalized_block:
            return candidates

        patterns = [
            (
                rf'artist\s+for\s+the\s+song\s+["“]?{title_pattern}["”]?\s+is\s+([^.\n\r|•·]+)',
                0.98,
                "google_answer",
            ),
            (
                rf'\bartist\s*[:\-–—]\s*([^.\n\r|•·]+)',
                0.94,
                "artist_label",
            ),
            (
                rf'{title_pattern}\s+(?:by|from)\s+([^.\n\r|•·]+)',
                0.90,
                "title_by_artist",
            ),
            (
                rf'{title_pattern}\s*[-–—]\s*([^.\n\r|•·]+)',
                0.88,
                "title_dash_artist",
            ),
            (
                rf'karaoke\s+{title_pattern}\s*[-–—]\s*([^.\n\r|•·\[]+)',
                0.88,
                "karaoke_title_dash_artist",
            ),
            (
                rf'([^.\n\r|•·]{{1,90}}?)\s*[-–—]\s*{title_pattern}',
                0.86,
                "artist_dash_title",
            ),
        ]
        for pattern, confidence, source in patterns:
            for match in re.finditer(pattern, block, flags=re.I):
                artist = cls._clean_artist(match.group(1), title)
                if artist:
                    candidates.append((artist, confidence, source))
        return candidates

    def _search_blocks(self, query: str) -> list[str]:
        self.rate_limiter.wait(self.provider_name)
        try:
            response = self.session.get(self.search_endpoint, params={"q": query}, timeout=(5, 12))
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            LOGGER.debug("artist search failed: %s", exc)
            return []
        if not response.ok:
            return []
        return self._result_blocks(response.text)[: self.max_results]

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        if not self.is_configured():
            return None

        votes: dict[str, dict[str, object]] = defaultdict(
            lambda: {"score": 0.0, "sources": set(), "examples": []}
        )
        for title in self._title_candidates(media):
            queries = [
                f'the artist for the song "{title}"',
                f'"{title}" artist song',
                f'"{title}" karaoke artist',
            ]
            for query in queries:
                for block in self._search_blocks(query):
                    for artist, confidence, source in self._artists_from_block(block, title):
                        key = normalize_text(artist)
                        if not key:
                            continue
                        item = votes[key]
                        item["score"] = float(item["score"]) + confidence
                        item["value"] = artist
                        item["sources"].add(source)
                        if len(item["examples"]) < 3:
                            item["examples"].append(block[:240])

        if not votes:
            return ProviderResult("artist_search", 0, {}, notes="no artist found from search results")

        best_key, best = max(
            votes.items(),
            key=lambda item: (float(item[1]["score"]), len(item[1]["sources"])),
        )
        score = float(best["score"])
        confidence = min(0.98, 0.72 + (score * 0.10))
        runner_up = max((float(item["score"]) for key, item in votes.items() if key != best_key), default=0.0)
        existing_artist = media.tags.get("artist", "")
        if existing_artist and similarity(existing_artist, str(best["value"])) >= 0.86:
            confidence = max(confidence, 0.90)
        elif runner_up and score - runner_up < 0.35:
            confidence = min(confidence, 0.74)

        if confidence < self.min_confidence:
            return ProviderResult(
                "artist_search",
                0,
                {},
                raw={"candidates": {key: {"score": item["score"]} for key, item in votes.items()}},
                notes="artist search candidates below confidence threshold",
            )

        return ProviderResult(
            "artist_search",
            confidence,
            {"artist": str(best["value"])},
            raw={
                "artist_key": best_key,
                "score": round(score, 3),
                "sources": sorted(best["sources"]),
                "examples": best["examples"],
            },
            notes="artist resolved from title-matching search result snippets",
        )
