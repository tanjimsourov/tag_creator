from __future__ import annotations

from urllib.parse import quote

from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class DeezerClient(ProviderClient):
    provider_name = "deezer"
    base_url = "https://api.deezer.com"

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        if not title and not artist:
            return None
        query = " ".join(item for item in [artist, title] if item)
        data = self.get_json(
            f"{self.base_url}/search",
            params={"q": query, "limit": 5},
            cache_key_extra="track-search",
        )
        tracks = data.get("data", []) if data else []
        if not tracks:
            return ProviderResult("deezer", 0, {}, notes="no match")
        track = tracks[0]
        plausible, title_score, artist_score = plausible_track_match(
            title,
            artist,
            track.get("title", ""),
            track.get("artist", {}).get("name", ""),
        )
        if not plausible:
            return ProviderResult(
                "deezer",
                0,
                {},
                notes=f"rejected low similarity title={title_score:.2f}, artist={artist_score:.2f}",
            )
        album_data = {}
        album_id = track.get("album", {}).get("id")
        if album_id:
            album_data = self.get_json(
                f"{self.base_url}/album/{quote(str(album_id))}",
                cache_key_extra="album",
            ) or {}
        fields = {
            "title": track.get("title", ""),
            "artist": track.get("artist", {}).get("name", ""),
            "album": track.get("album", {}).get("title", ""),
            "album_artist": album_data.get("artist", {}).get("name", ""),
            "genre": album_data.get("genres", {}).get("data", [{}])[0].get("name", "")
            if album_data.get("genres", {}).get("data")
            else "",
            "date": album_data.get("release_date", ""),
            "year": (album_data.get("release_date", "") or "")[:4],
            "track_number": str(track.get("track_position") or ""),
            "disc_number": str(track.get("disk_number") or ""),
            "bpm": str(track.get("bpm") or ""),
            "cover_art_url": track.get("album", {}).get("cover_xl", "") or track.get("album", {}).get("cover_big", ""),
        }
        return ProviderResult(
            "deezer",
            0.74,
            {key: value for key, value in fields.items() if value},
            source_url=track.get("link", ""),
            raw={"track_id": track.get("id", ""), "album_id": album_id or ""},
            notes=f"Deezer public API; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
