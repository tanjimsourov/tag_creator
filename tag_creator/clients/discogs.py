from __future__ import annotations

from ..config import Settings
from ..matching import similarity
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs
from .base import ProviderClient


class DiscogsClient(ProviderClient):
    provider_name = "discogs"
    base_url = "https://api.discogs.com"

    def __init__(self, db, rate_limiter, settings: Settings) -> None:
        super().__init__(db, rate_limiter)
        self.token = settings.discogs_token
        self.consumer_key = settings.discogs_consumer_key
        self.consumer_secret = settings.discogs_consumer_secret

    def is_configured(self) -> bool:
        return bool(self.token or (self.consumer_key and self.consumer_secret))

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        album = media.tags.get("album", "")
        candidates = candidate_track_pairs(media, limit=3)
        if not self.is_configured() or not candidates:
            return None
        ranked = []
        for artist, title in candidates:
            query = " ".join(item for item in [artist, album or title] if item)
            params = {"q": query, "type": "release", "per_page": 8}
            if self.token:
                params["token"] = self.token
            else:
                params["key"] = self.consumer_key
                params["secret"] = self.consumer_secret
            data = self.get_json(
                f"{self.base_url}/database/search",
                params=params,
                headers={"User-Agent": "SMC-Tag-Creator/0.1"},
                cache_key_extra="release-search",
            )
            for candidate in (data.get("results", []) if data else []):
                candidate_title = candidate.get("title", "")
                candidate_artist = candidate_title.split(" - ", 1)[0] if " - " in candidate_title else ""
                candidate_album = candidate_title.split(" - ", 1)[-1]
                album_score = similarity(album or title, candidate_album)
                artist_score = similarity(artist, candidate_artist) if artist and candidate_artist else 0.55
                if album_score >= 0.45 and artist_score >= 0.40:
                    ranked.append(((album_score * 0.65) + (artist_score * 0.35), album_score, artist_score, query, candidate))
        if not ranked:
            return ProviderResult("discogs", 0, {}, notes="no match")

        _, album_score, artist_score, query, release = sorted(ranked, key=lambda item: item[0], reverse=True)[0]
        fields = {
            "album": release.get("title", "").split(" - ", 1)[-1],
            "year": str(release.get("year") or ""),
            "genre": ", ".join(release.get("genre", []) or release.get("style", []) or []),
            "label": ", ".join(release.get("label", []) or []),
            "catalog_number": ", ".join(release.get("catno", []) if isinstance(release.get("catno"), list) else [str(release.get("catno", ""))]),
            "cover_art_url": release.get("cover_image", ""),
        }
        fields = {key: value for key, value in fields.items() if value}
        return ProviderResult(
            "discogs",
            0.72,
            fields,
            source_url=f"https://www.discogs.com{release.get('uri', '')}",
            raw={"discogs_id": release.get("id", "")},
            notes=f"database release search; query={query}; album_similarity={album_score:.2f}; artist_similarity={artist_score:.2f}",
        )
