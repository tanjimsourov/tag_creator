from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..models import MediaFile, ProviderResult
from .base import ProviderClient


class SonotellerClient(ProviderClient):
    """SONOTELLER paid AI-analysis adapter through RapidAPI.

    Public RapidAPI examples indicate a JSON body containing a public file URL:
    {"file": "https://.../song.mp3"}. The exact paid-plan endpoint behavior
    should be confirmed after subscription; endpoint and host are .env-driven.
    """

    provider_name = "sonoteller"

    def __init__(self, store, rate_limiter, settings: Settings) -> None:
        super().__init__(store, rate_limiter)
        self.api_key = settings.sonoteller_rapidapi_key
        self.host = settings.sonoteller_rapidapi_host
        self.base_url = settings.sonoteller_base_url
        self.endpoint = settings.sonoteller_analyze_endpoint
        self.input_mode = settings.sonoteller_input_mode
        self.file_url_base = settings.sonoteller_file_url_base

    def is_configured(self) -> bool:
        return bool(self.api_key and self.host and self.base_url and self.endpoint)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": self.host,
        }

    def _public_file_url(self, path: Path) -> str:
        if not self.file_url_base:
            return ""
        return f"{self.file_url_base}/{quote(path.name)}"

    @staticmethod
    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(item) for item in value if item not in {None, ""})
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    @classmethod
    def _first(cls, data: dict[str, Any], *paths: str) -> str:
        for path in paths:
            current: Any = data
            for part in path.split("."):
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    current = None
                    break
            text = cls._as_text(current)
            if text:
                return text
        return ""

    def _map_response(self, data: dict[str, Any]) -> dict[str, str]:
        # Keep mapping broad because paid API response schemas can vary by
        # endpoint/version. The raw JSON is also retained in analysis_json.
        root = data.get("result") if isinstance(data.get("result"), dict) else data
        fields = {
            "title": self._first(root, "title", "song.title", "metadata.title"),
            "artist": self._first(root, "artist", "artists", "song.artist", "metadata.artist"),
            "album": self._first(root, "album", "metadata.album"),
            "genre": self._first(root, "genre", "genres", "primary_genre", "analysis.genre"),
            "subgenre": self._first(root, "subgenre", "subgenres", "secondary_genre", "analysis.subgenre"),
            "mood": self._first(root, "mood", "primary_mood", "analysis.mood"),
            "moods": self._first(root, "moods", "mood_tags", "analysis.moods"),
            "bpm": self._first(root, "bpm", "tempo", "analysis.bpm"),
            "key": self._first(root, "key", "musical_key", "analysis.key"),
            "language": self._first(root, "language", "lyrics_language", "analysis.language"),
            "energy": self._first(root, "energy", "analysis.energy"),
            "valence": self._first(root, "valence", "analysis.valence"),
            "danceability": self._first(root, "danceability", "analysis.danceability"),
            "instruments": self._first(root, "instruments", "instrumentation", "analysis.instruments"),
            "vocals": self._first(root, "vocals", "voice", "analysis.vocals"),
            "themes": self._first(root, "themes", "theme", "analysis.themes"),
            "analysis_summary": self._first(root, "summary", "description", "analysis.summary"),
            "analysis_json": json.dumps(data, ensure_ascii=False),
        }
        return {key: value for key, value in fields.items() if value}

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        if not self.is_configured():
            return None

        if self.input_mode != "url":
            return ProviderResult(
                "sonoteller",
                0,
                {},
                notes=f"input mode {self.input_mode!r} not implemented; use url mode or add endpoint contract",
            )

        file_url = self._public_file_url(media.path)
        if not file_url:
            return ProviderResult(
                "sonoteller",
                0,
                {},
                notes="SONOTELLER_FILE_URL_BASE is required because RapidAPI endpoint expects a public file URL",
            )

        endpoint = self.endpoint if self.endpoint.startswith("/") else f"/{self.endpoint}"
        data = self.post_json(
            f"{self.base_url}{endpoint}",
            payload={"file": file_url},
            headers=self._headers(),
            cache_key_extra=str(media.path),
        )
        if not data:
            return ProviderResult("sonoteller", 0, {}, notes="no response or request failed")
        fields = self._map_response(data)
        if not fields:
            return ProviderResult("sonoteller", 0.30, {}, raw=data, notes="response had no mapped fields")
        return ProviderResult(
            "sonoteller",
            0.92,
            fields,
            source_url=file_url,
            raw={"endpoint": endpoint},
            notes="paid AI analysis",
        )
