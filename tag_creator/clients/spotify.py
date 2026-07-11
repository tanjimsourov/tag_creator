from __future__ import annotations

import base64
import time

import requests

from ..config import Settings
from ..matching import plausible_track_match
from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class SpotifyClient(ProviderClient):
    provider_name = "spotify"
    base_url = "https://api.spotify.com/v1"

    def __init__(self, db, rate_limiter, settings: Settings) -> None:
        super().__init__(db, rate_limiter)
        self.client_id = settings.spotify_client_id
        self.client_secret = settings.spotify_client_secret
        self._token = ""
        self._token_expires_at = 0.0

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _token_header(self) -> dict[str, str] | None:
        if not self.is_configured():
            return None
        if self._token and time.time() < self._token_expires_at - 60:
            return {"Authorization": f"Bearer {self._token}"}
        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        response = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {credentials}"},
            timeout=30,
        )
        if not response.ok:
            return None
        data = response.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        title = media.tags.get("title", "")
        artist = media.tags.get("artist", "")
        if not title or not artist or not self.is_configured():
            return None
        headers = self._token_header()
        if not headers:
            return ProviderResult("spotify", 0, {}, notes="token unavailable")

        query = f'track:"{title}" artist:"{artist}"'
        data = self.get_json(
            f"{self.base_url}/search",
            params={"q": query, "type": "track", "limit": 5},
            headers=headers,
            cache_key_extra="track-search",
        )
        tracks = data.get("tracks", {}).get("items", []) if data else []
        if not tracks:
            return ProviderResult("spotify", 0, {}, notes="no match")

        ranked = []
        for candidate in tracks:
            candidate_artist = ", ".join(artist_item.get("name", "") for artist_item in candidate.get("artists", []))
            plausible, title_score, artist_score = plausible_track_match(
                title,
                artist,
                candidate.get("name", ""),
                candidate_artist,
            )
            if plausible:
                popularity = int(candidate.get("popularity") or 0)
                ranked.append(((title_score * 0.55) + (artist_score * 0.35) + (popularity / 100 * 0.10), title_score, artist_score, candidate))
        if not ranked:
            return ProviderResult("spotify", 0, {}, notes="rejected all results by title/artist similarity")

        _, title_score, artist_score, track = sorted(ranked, key=lambda item: item[0], reverse=True)[0]
        album = track.get("album", {})
        fields = {
            "title": track.get("name", ""),
            "artist": ", ".join(artist_item.get("name", "") for artist_item in track.get("artists", [])),
            "album": album.get("name", ""),
            "album_artist": ", ".join(artist_item.get("name", "") for artist_item in album.get("artists", [])),
            "date": album.get("release_date", ""),
            "year": (album.get("release_date", "") or "")[:4],
            "track_number": str(track.get("track_number") or ""),
            "disc_number": str(track.get("disc_number") or ""),
            "isrc": track.get("external_ids", {}).get("isrc", ""),
        }
        images = album.get("images", [])
        if images:
            fields["cover_art_url"] = images[0].get("url", "")
        fields = {key: value for key, value in fields.items() if value}
        popularity = int(track.get("popularity") or 0)
        confidence = 0.72 + min(popularity / 100, 1) * 0.18
        return ProviderResult(
            "spotify",
            confidence,
            fields,
            source_url=track.get("external_urls", {}).get("spotify", ""),
            raw={"spotify_id": track.get("id", ""), "popularity": popularity},
            notes=f"popularity {popularity}; title_similarity={title_score:.2f}; artist_similarity={artist_score:.2f}",
        )
