from __future__ import annotations

from ..config import Settings
from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class LastFMClient(ProviderClient):
    provider_name = "lastfm"
    base_url = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, db, rate_limiter, settings: Settings) -> None:
        super().__init__(db, rate_limiter)
        self.api_key = settings.lastfm_api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        if not title or not artist or not self.is_configured():
            return None
        data = self.get_json(
            self.base_url,
            params={
                "method": "track.getInfo",
                "api_key": self.api_key,
                "artist": artist,
                "track": title,
                "format": "json",
            },
            cache_key_extra="track-info",
        )
        track = data.get("track", {}) if data else {}
        if not track:
            return ProviderResult("lastfm", 0, {}, notes="no match")
        plausible, title_score, artist_score = plausible_track_match(
            title,
            artist,
            track.get("name", ""),
            track.get("artist", {}).get("name", ""),
        )
        if not plausible:
            return ProviderResult(
                "lastfm",
                0,
                {},
                notes=f"rejected low similarity title={title_score:.2f}, artist={artist_score:.2f}",
            )
        tags = track.get("toptags", {}).get("tag", [])
        genre = tags[0].get("name", "") if tags else ""
        fields = {
            "title": track.get("name", ""),
            "artist": track.get("artist", {}).get("name", ""),
            "album": track.get("album", {}).get("title", ""),
            "genre": genre,
        }
        images = track.get("album", {}).get("image", [])
        usable_images = [image.get("#text", "") for image in images if image.get("#text")]
        if usable_images:
            fields["cover_art_url"] = usable_images[-1]
        fields = {key: value for key, value in fields.items() if value}
        return ProviderResult(
            "lastfm",
            0.68,
            fields,
            source_url=track.get("url", ""),
            notes=f"track.getInfo; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
