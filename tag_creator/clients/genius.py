from __future__ import annotations

from ..config import Settings
from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs
from .base import ProviderClient


class GeniusClient(ProviderClient):
    provider_name = "genius"
    base_url = "https://api.genius.com"

    def __init__(self, db, rate_limiter, settings: Settings) -> None:
        super().__init__(db, rate_limiter)
        self.token = settings.genius_access_token
        self.client_id = settings.genius_client_id
        self.client_secret = settings.genius_client_secret

    def is_configured(self) -> bool:
        return bool(self.token)

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        candidates = candidate_track_pairs(media, limit=3)
        if not self.is_configured() or not candidates:
            return None
        ranked = []
        for artist, title in candidates:
            query = " ".join(item for item in [artist, title] if item)
            data = self.get_json(
                f"{self.base_url}/search",
                params={"q": query},
                headers={"Authorization": f"Bearer {self.token}"},
                cache_key_extra="search",
            )
            hits = data.get("response", {}).get("hits", []) if data else []
            for hit in hits[:8]:
                candidate = hit.get("result", {})
                plausible, title_score, artist_score = plausible_track_match(
                    title,
                    artist,
                    candidate.get("title", ""),
                    candidate.get("primary_artist", {}).get("name", ""),
                )
                if plausible:
                    ranked.append(((title_score * 0.6) + (artist_score * 0.4), title_score, artist_score, query, candidate))
        if not ranked:
            return ProviderResult("genius", 0, {}, notes="no match")
        _, title_score, artist_score, query, song = sorted(ranked, key=lambda item: item[0], reverse=True)[0]
        fields = {
            "title": song.get("title", ""),
            "artist": song.get("primary_artist", {}).get("name", ""),
        }
        # Genius API search provides URLs and basic metadata. Do not scrape lyrics.
        return ProviderResult(
            "genius",
            0.50,
            {key: value for key, value in fields.items() if value},
            source_url=song.get("url", ""),
            raw={"genius_id": song.get("id", "")},
            notes=f"metadata only; lyrics scraping disabled; query={query}; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
