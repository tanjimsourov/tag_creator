from __future__ import annotations

from ..models import MediaFile, ProviderResult
from ..matching import plausible_track_match
from .base import ProviderClient


class ITunesClient(ProviderClient):
    provider_name = "itunes"
    base_url = "https://itunes.apple.com/search"

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        if not title and not artist:
            return None
        query = " ".join(item for item in [artist, title] if item)
        data = self.get_json(
            self.base_url,
            params={"term": query, "media": "music", "entity": "song", "limit": 5},
            cache_key_extra="song-search",
        )
        results = data.get("results", []) if data else []
        if not results:
            return ProviderResult("itunes", 0, {}, notes="no match")
        item = results[0]
        plausible, title_score, artist_score = plausible_track_match(
            title,
            artist,
            item.get("trackName", ""),
            item.get("artistName", ""),
        )
        if not plausible:
            return ProviderResult(
                "itunes",
                0,
                {},
                notes=f"rejected low similarity title={title_score:.2f}, artist={artist_score:.2f}",
            )
        artwork = item.get("artworkUrl100", "")
        if artwork:
            artwork = artwork.replace("100x100bb", "600x600bb")
        fields = {
            "title": item.get("trackName", ""),
            "artist": item.get("artistName", ""),
            "album": item.get("collectionName", ""),
            "album_artist": item.get("collectionArtistName", "") or item.get("artistName", ""),
            "genre": item.get("primaryGenreName", ""),
            "date": item.get("releaseDate", ""),
            "year": (item.get("releaseDate", "") or "")[:4],
            "track_number": str(item.get("trackNumber") or ""),
            "disc_number": str(item.get("discNumber") or ""),
            "cover_art_url": artwork,
        }
        return ProviderResult(
            "itunes",
            0.78,
            {key: value for key, value in fields.items() if value},
            source_url=item.get("trackViewUrl", ""),
            raw={"track_id": item.get("trackId", "")},
            notes=f"Apple iTunes Search API; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
