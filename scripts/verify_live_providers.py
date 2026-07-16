#!/usr/bin/env python3
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings


def _status(name: str, ok: bool, message: str) -> bool:
    print(f"{name}: {'ok' if ok else 'failed'} - {message}")
    return ok


def _skip(name: str, message: str) -> bool:
    print(f"{name}: skipped - {message}")
    return True


def _enabled(settings, name: str) -> bool:
    return name in set(settings.enabled_providers)


def check_spotify(settings) -> bool:
    if not _enabled(settings, "spotify"):
        return _skip("spotify", "not enabled for this run")
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
    if not _enabled(settings, "lastfm"):
        return _skip("lastfm", "not enabled for this run")
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
    if not _enabled(settings, "discogs"):
        return _skip("discogs", "not enabled for this run")
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


def check_acoustid(settings) -> bool:
    if not _enabled(settings, "acoustid"):
        return _skip("acoustid", "not enabled for this run")
    if not settings.acoustid_api_key:
        return _status("acoustid", False, "missing ACOUSTID_API_KEY")
    fpcalc = settings.fpcalc_path or shutil.which("fpcalc") or ""
    if not fpcalc:
        return _status("acoustid", False, "missing FPCALC_PATH/fpcalc executable")
    try:
        version = subprocess.run([fpcalc, "-version"], text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return _status("acoustid", False, f"fpcalc unavailable: {exc}")
    if version.returncode != 0:
        return _status("acoustid", False, "fpcalc returned non-zero exit")
    try:
        response = requests.get(
            "https://api.acoustid.org/v2/lookup",
            params={"client": settings.acoustid_api_key, "duration": 1, "fingerprint": "AAAA"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return _status("acoustid", False, str(exc))
    text = response.text[:220]
    if "invalid api key" in text.lower():
        return _status("acoustid", False, f"invalid API key: {text}")
    if response.status_code == 400 and "invalid fingerprint" in text.lower():
        return _status("acoustid", True, "API key reachable and fpcalc available; dummy fingerprint was rejected as expected")
    if not response.ok:
        return _status("acoustid", False, f"{response.status_code}: {text}")
    return _status("acoustid", True, "API key accepted and fpcalc available")


def check_genius(settings) -> bool:
    if not _enabled(settings, "genius"):
        return _skip("genius", "not enabled for this run")
    if not settings.genius_access_token:
        return _status("genius", False, "missing GENIUS_ACCESS_TOKEN")
    try:
        response = requests.get(
            "https://api.genius.com/search",
            params={"q": "Coldplay Yellow"},
            headers={"Authorization": f"Bearer {settings.genius_access_token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return _status("genius", False, str(exc))
    if not response.ok:
        return _status("genius", False, f"{response.status_code}: {response.text[:220]}")
    return _status("genius", True, "sample metadata search accepted")


def main() -> int:
    settings = load_settings()
    checks = [
        check_spotify(settings),
        check_lastfm(settings),
        check_discogs(settings),
        check_acoustid(settings),
        check_genius(settings),
    ]
    return 0 if all(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
