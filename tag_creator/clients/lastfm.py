from __future__ import annotations

from ..config import Settings
from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs
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
        candidates = [(artist, title) for artist, title in candidate_track_pairs(media, limit=3) if artist and title]
        if not candidates or not self.is_configured():
            return None

        best = None
        for artist, title in candidates:
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
                continue
            plausible, title_score, artist_score = plausible_track_match(
                title,
                artist,
                track.get("name", ""),
                track.get("artist", {}).get("name", ""),
            )
            if plausible:
                score = (title_score * 0.60) + (artist_score * 0.40)
                if not best or score > best[0]:
                    best = (score, title_score, artist_score, artist, title, track)
        if not best:
            return ProviderResult("lastfm", 0, {}, notes="no match")

        _, title_score, artist_score, artist, title, track = best
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
            notes=f"track.getInfo; query={artist} {title}; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
