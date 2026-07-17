from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import quote

from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class MusicBrainzClient(ProviderClient):
    provider_name = "musicbrainz"
    base_url = "https://musicbrainz.org/ws/2"

    def __init__(self, db, rate_limiter) -> None:
        super().__init__(db, rate_limiter)
        self.headers = {"User-Agent": "SMC-Tag-Creator/0.1 (metadata-enrichment)"}

    @staticmethod
    def _clean_title(title: str) -> str:
        title = re.sub(r"\[[^\]]*(official|video|audio|lyrics?|visualizer)[^\]]*\]", "", title, flags=re.I)
        title = re.sub(r"\([^\)]*(official|video|audio|lyrics?|visualizer)[^\)]*\)", "", title, flags=re.I)
        return re.sub(r"\s+", " ", title).strip()

    def _search_recordings(self, queries: list[str]) -> list[dict]:
        for query in queries:
            if not query.strip():
                continue
            data = self.get_json(
                f"{self.base_url}/recording",
                params={"fmt": "json", "limit": 5, "query": query},
                headers=self.headers,
                cache_key_extra="recording-search",
            )
            recordings = data.get("recordings", []) if data else []
            if recordings:
                return recordings
        return []

    @staticmethod
    def _norm(value: str) -> str:
        value = value.lower()
        value = re.sub(r"\[[^\]]+\]|\([^\)]+\)", " ", value)
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _similarity(self, left: str, right: str) -> float:
        left_norm = self._norm(self._clean_title(left))
        right_norm = self._norm(self._clean_title(right))
        if not left_norm or not right_norm:
            return 0.0
        if left_norm in right_norm or right_norm in left_norm:
            shorter = min(len(left_norm), len(right_norm))
            longer = max(len(left_norm), len(right_norm))
            return max(0.70, shorter / longer)
        return SequenceMatcher(None, left_norm, right_norm).ratio()

    def _recording_artist(self, recording: dict) -> str:
        if not recording.get("artist-credit"):
            return ""
        return "".join(part.get("name", "") + part.get("joinphrase", "") for part in recording["artist-credit"]).strip()

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        album = media.tags.get("album", "")
        isrc = media.tags.get("isrc", "")
        if not title and not isrc:
            return None

        recordings = []
        if isrc:
            data = self.get_json(
                f"{self.base_url}/isrc/{quote(isrc)}",
                params={"fmt": "json", "inc": "recordings+artists+releases+isrcs+genres+tags"},
                headers=self.headers,
                cache_key_extra="isrc",
            )
            recordings = data.get("recordings", []) if data else []

        if not recordings and artist:
            cleaned_title = self._clean_title(title)
            strict_parts = []
            if title:
                strict_parts.append(f'recording:"{title}"')
            if artist:
                strict_parts.append(f'artist:"{artist}"')
            if album:
                strict_parts.append(f'release:"{album}"')
            cleaned_parts = []
            if cleaned_title:
                cleaned_parts.append(f'recording:"{cleaned_title}"')
            if artist:
                cleaned_parts.append(f'artist:"{artist}"')
            loose_query = " ".join(item for item in [cleaned_title or title, artist] if item)
            recordings = self._search_recordings(
                [
                    " AND ".join(strict_parts),
                    " AND ".join(cleaned_parts),
                    loose_query,
                ]
            )

        if not recordings:
            return ProviderResult("musicbrainz", 0, {}, notes="no match")

        ranked = []
        for recording in recordings:
            api_score = int(recording.get("score", 80)) / 100
            title_similarity = self._similarity(title, recording.get("title", ""))
            artist_similarity = self._similarity(artist, self._recording_artist(recording)) if artist else 0.65
            local_score = (title_similarity * 0.60) + (artist_similarity * 0.40)
            combined = (api_score * 0.45) + (local_score * 0.55)
            ranked.append((combined, title_similarity, artist_similarity, api_score, recording))

        combined, title_similarity, artist_similarity, api_score, best = sorted(
            ranked, key=lambda item: item[0], reverse=True
        )[0]
        if title_similarity < 0.45 or (artist and artist_similarity < 0.45):
            return ProviderResult(
                "musicbrainz",
                0,
                {},
                notes=(
                    "rejected low local similarity "
                    f"title={title_similarity:.2f}, artist={artist_similarity:.2f}, api={api_score:.2f}"
                ),
            )

        score = min(0.98, combined)
        fields: dict[str, str] = {}
        fields["title"] = best.get("title", "")
        if best.get("artist-credit"):
            fields["artist"] = self._recording_artist(best)
            fields["album_artist"] = fields["artist"]
        if best.get("isrcs"):
            fields["isrc"] = best["isrcs"][0]

        genres = best.get("genres") or best.get("tags") or []
        if genres:
            fields["genre"] = sorted(genres, key=lambda item: int(item.get("count", 0)), reverse=True)[0].get("name", "")

        releases = best.get("releases", [])
        release_mbid = ""
        if releases:
            release = releases[0]
            fields["album"] = release.get("title", "")
            if release.get("date"):
                fields["date"] = release["date"]
                fields["year"] = release["date"][:4]
            release_mbid = release.get("id", "")
        elif best.get("first-release-date"):
            fields["date"] = best["first-release-date"]
            fields["year"] = best["first-release-date"][:4]

        fields = {key: value for key, value in fields.items() if value}
        raw = {"recording_id": best.get("id", ""), "release_mbid": release_mbid}
        source = f"https://musicbrainz.org/recording/{best.get('id')}" if best.get("id") else ""
        return ProviderResult(
            "musicbrainz",
            max(0.40, min(score, 0.98)),
            fields,
            source_url=source,
            raw=raw,
            notes=f"score {score:.2f}, title_similarity {title_similarity:.2f}, artist_similarity {artist_similarity:.2f}",
        )
