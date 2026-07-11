#!/usr/bin/env python3
from __future__ import annotations

import base64
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings


def _status(name: str, ok: bool, message: str) -> bool:
    print(f"{name}: {'ok' if ok else 'failed'} - {message}")
    return ok


def check_spotify(settings) -> bool:
    if not (settings.spotify_client_id and settings.spotify_client_secret):
        return _status("spotify", False, "missing SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET")
    token = base64.b64encode(f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()).decode()
    try:
        response = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return _status("spotify", False, str(exc))
    if not response.ok:
        return _status("spotify", False, f"{response.status_code}: {response.text[:220]}")
    access_token = response.json().get("access_token", "")
    try:
        search = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": 'track:"Yellow" artist:"Coldplay"', "type": "track", "limit": 1},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return _status("spotify", False, str(exc))
    if not search.ok:
        return _status("spotify", False, f"token ok, search failed {search.status_code}: {search.text[:220]}")
    return _status("spotify", True, "token and sample track search accepted")


def check_lastfm(settings) -> bool:
    if not settings.lastfm_api_key:
        return _status("lastfm", False, "missing LASTFM_API_KEY")
    try:
        response = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={"method": "artist.getinfo", "artist": "Coldplay", "api_key": settings.lastfm_api_key, "format": "json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return _status("lastfm", False, str(exc))
    if not response.ok or "error" in response.text[:80].lower():
        return _status("lastfm", False, f"{response.status_code}: {response.text[:220]}")
    return _status("lastfm", True, "sample artist lookup accepted")


def check_discogs(settings) -> bool:
    if not (settings.discogs_token or (settings.discogs_consumer_key and settings.discogs_consumer_secret)):
        return _status("discogs", False, "missing DISCOGS_TOKEN or DISCOGS_CONSUMER_KEY/SECRET")
    params = {"q": "Daft Punk", "type": "release", "per_page": 1}
    if settings.discogs_token:
        params["token"] = settings.discogs_token
    else:
        params["key"] = settings.discogs_consumer_key
        params["secret"] = settings.discogs_consumer_secret
    try:
        response = requests.get(
            "https://api.discogs.com/database/search",
            params=params,
            headers={"User-Agent": "SMC-Tag-Creator/0.1"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return _status("discogs", False, str(exc))
    if not response.ok:
        return _status("discogs", False, f"{response.status_code}: {response.text[:220]}")
    return _status("discogs", True, "sample database search accepted")


def main() -> int:
    settings = load_settings()
    checks = [
        check_spotify(settings),
        check_lastfm(settings),
        check_discogs(settings),
    ]
    return 0 if all(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
