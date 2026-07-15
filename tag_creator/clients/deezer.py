from __future__ import annotations

from urllib.parse import quote

from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from ..querying import candidate_track_pairs
from .base import ProviderClient


class DeezerClient(ProviderClient):
    provider_name = "deezer"
    base_url = "https://api.deezer.com"

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        candidates = candidate_track_pairs(media, limit=3)
        if not candidates:
            return None
        ranked = []
        for artist, title in candidates:
            query = " ".join(item for item in [artist, title] if item)
            data = self.get_json(
                f"{self.base_url}/search",
                params={"q": query, "limit": 8},
                cache_key_extra="track-search",
            )
            for track in (data.get("data", []) if data else []):
                plausible, title_score, artist_score = plausible_track_match(
                    title,
                    artist,
                    track.get("title", ""),
                    track.get("artist", {}).get("name", ""),
                )
                if plausible:
                    ranked.append(((title_score * 0.60) + (artist_score * 0.40), title_score, artist_score, query, track))
        if not ranked:
            return ProviderResult("deezer", 0, {}, notes="no match")
        _, title_score, artist_score, query, track = sorted(ranked, key=lambda row: row[0], reverse=True)[0]
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
            notes=f"Deezer public API; query={query}; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
