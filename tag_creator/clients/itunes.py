from __future__ import annotations

from ..models import MediaFile, ProviderResult
from ..matching import plausible_track_match
from ..querying import candidate_track_pairs
from .base import ProviderClient


class ITunesClient(ProviderClient):
    provider_name = "itunes"
    base_url = "https://itunes.apple.com/search"

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        candidates = candidate_track_pairs(media, limit=3)
        if not candidates:
            return None
        ranked = []
        for artist, title in candidates:
            if not artist:
                continue
            query = " ".join(item for item in [artist, title] if item)
            data = self.get_json(
                self.base_url,
                params={"term": query, "media": "music", "entity": "song", "limit": 8},
                cache_key_extra="song-search",
            )
            for item in (data.get("results", []) if data else []):
                plausible, title_score, artist_score = plausible_track_match(
                    title,
                    artist,
                    item.get("trackName", ""),
                    item.get("artistName", ""),
                )
                if plausible:
                    ranked.append(((title_score * 0.60) + (artist_score * 0.40), title_score, artist_score, query, item))
        if not ranked:
            return ProviderResult("itunes", 0, {}, notes="no match")
        _, title_score, artist_score, query, item = sorted(ranked, key=lambda row: row[0], reverse=True)[0]
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
            notes=f"Apple iTunes Search API; query={query}; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
