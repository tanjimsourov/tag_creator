from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)
_DEFAULT_CATALOG: GenreCatalog | None = None


def _clean(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _title_case(value: str) -> str:
    cleaned = _clean(value)
    if not cleaned:
        return ""
    keep_upper = {"ai", "dj", "edm", "r&b", "rnb", "uk", "us", "usa"}
    words = []
    for word in cleaned.split():
        lower = word.lower()
        words.append(lower.upper() if lower in keep_upper else word.capitalize())
    return " ".join(words)


def genre_key(value: str) -> str:
    cleaned = _clean(value).lower().replace("&", " and ")
    cleaned = cleaned.replace("hiphop", "hip hop").replace("rnb", "r and b")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return " ".join(cleaned.split())


def split_genres(value: str) -> list[str]:
    cleaned = _clean(value)
    if not cleaned:
        return []
    parts: list[str] = []
    for comma_part in cleaned.split(","):
        parts.extend(comma_part.split(";"))
    return [_clean(part) for part in parts if _clean(part)]


@dataclass(frozen=True)
class GenreApiSettings:
    enabled: bool = True
    url: str = "https://applicationaddons.com/api/GetGenres"
    media_type: str = "Audio"
    db_id: str = "0"
    lang: str = ""
    timeout_seconds: int = 30


class GenreCatalog:
    """Approved portal genre names loaded from the provided genre API.

    Rule from Talwinder:
    - match AI/free-source genre against the API/DB response first
    - if matched, use the API DisplayName exactly
    - if not matched, keep the AI genre but Title Case it
    """

    def __init__(self, settings: GenreApiSettings) -> None:
        self.settings = settings
        self._loaded = False
        self._names_by_key: dict[str, str] = {}

    def normalize(self, value: str) -> str:
        parts = split_genres(value)
        if not parts:
            return ""
        self._ensure_loaded()

        normalized: list[str] = []
        seen: set[str] = set()
        for part in parts:
            mapped = self._match_display_name(part) if self.settings.enabled else None
            final_value = mapped or _title_case(part)
            final_key = genre_key(final_value)
            if final_value and final_key not in seen:
                seen.add(final_key)
                normalized.append(final_value)
        return ", ".join(normalized)

    def _match_display_name(self, value: str) -> str:
        key = genre_key(value)
        if not key:
            return ""
        exact = self._names_by_key.get(key)
        if exact:
            return exact

        # Not strict equality: compare normalized/tokenized values so AI output
        # with different case, punctuation, or small suffixes still maps to the
        # approved portal genre name.
        key_tokens = set(key.split())
        if not key_tokens:
            return ""
        best_name = ""
        best_score = 0.0
        for db_key, display_name in self._names_by_key.items():
            db_tokens = set(db_key.split())
            if not db_tokens:
                continue
            shared = key_tokens & db_tokens
            if not shared:
                continue
            score = len(shared) / max(len(key_tokens), len(db_tokens))
            if key in db_key or db_key in key:
                score += 0.25
            if score > best_score:
                best_score = score
                best_name = display_name
        return best_name if best_score >= 0.80 else ""

    def _ensure_loaded(self) -> None:
        if self._loaded or not self.settings.enabled:
            return
        self._loaded = True
        try:
            response = requests.post(
                self.settings.url,
                json={
                    "name": self.settings.media_type,
                    "id": self.settings.db_id,
                    "lang": self.settings.lang,
                },
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - fallback keeps the run usable
            LOGGER.warning("genre API unavailable; using Title Case fallback: %s", exc)
            return

        if str(payload.get("response", "")).strip() != "1":
            LOGGER.warning("genre API returned unsuccessful response; using Title Case fallback")
            return

        items = self._parse_items(payload.get("data"))
        for item in items:
            display_name = _clean(str(item.get("DisplayName", "")))
            if display_name:
                self._names_by_key.setdefault(genre_key(display_name), display_name)
        LOGGER.info("loaded %s approved genre names from genre API", len(self._names_by_key))

    @staticmethod
    def _parse_items(raw_data: Any) -> list[dict[str, Any]]:
        if isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
            except ValueError:
                return []
        else:
            parsed = raw_data
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_catalog() -> GenreCatalog:
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = GenreCatalog(
            GenreApiSettings(
                enabled=_env_bool("GENRE_API_ENABLED", True),
                url=os.environ.get("GENRE_API_URL", GenreApiSettings.url).strip() or GenreApiSettings.url,
                media_type=os.environ.get("GENRE_API_MEDIA_TYPE", "Audio").strip() or "Audio",
                db_id=os.environ.get("GENRE_API_ID", "0").strip() or "0",
                lang=os.environ.get("GENRE_API_LANG", ""),
                timeout_seconds=max(1, int(os.environ.get("GENRE_API_TIMEOUT_SECONDS", "30") or "30")),
            )
        )
    return _DEFAULT_CATALOG


def normalize_genre_name(value: str) -> str:
    return default_catalog().normalize(value)
