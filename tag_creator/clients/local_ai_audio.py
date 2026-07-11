from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import MediaFile, ProviderResult
from ..resource_limits import thread_limited_env
from .base import ProviderClient


def _sha256_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _split_label(label: str) -> str:
    label = label.strip()
    for separator in ("---", "::", ":", "/", "|"):
        if separator in label:
            label = label.split(separator)[-1]
    return label.replace("_", " ").replace("-", " ").strip()


def _pick(tags: list[dict[str, Any]], keywords: set[str], limit: int = 5) -> list[str]:
    picked: list[str] = []
    for item in tags:
        label = _split_label(str(item.get("label", "")))
        normalized = label.lower()
        if not label:
            continue
        if any(keyword in normalized for keyword in keywords) and label not in picked:
            picked.append(label)
        if len(picked) >= limit:
            break
    return picked


def _field_map(tags: list[dict[str, Any]], min_score: float) -> dict[str, str]:
    filtered = [item for item in tags if float(item.get("score") or 0) >= min_score]
    if not filtered:
        return {}

    genre_keywords = {
        "pop",
        "rock",
        "hip hop",
        "rap",
        "dance",
        "electronic",
        "house",
        "techno",
        "metal",
        "jazz",
        "classical",
        "country",
        "reggae",
        "latin",
        "r&b",
        "soul",
        "folk",
        "indie",
        "ambient",
        "blues",
    }
    mood_keywords = {
        "happy",
        "sad",
        "angry",
        "relaxed",
        "calm",
        "aggressive",
        "dark",
        "bright",
        "party",
        "romantic",
        "melancholic",
        "energetic",
        "uplifting",
        "chill",
    }
    instrument_keywords = {
        "guitar",
        "piano",
        "drums",
        "bass",
        "synth",
        "violin",
        "strings",
        "brass",
        "sax",
        "trumpet",
        "vocal",
    }
    theme_keywords = {"summer", "christmas", "love", "workout", "club", "background", "dance", "party"}

    genres = _pick(filtered, genre_keywords, 4)
    moods = _pick(filtered, mood_keywords, 6)
    instruments = _pick(filtered, instrument_keywords, 6)
    themes = _pick(filtered, theme_keywords, 5)
    fields: dict[str, str] = {}
    if genres:
        fields["genre"] = genres[0]
        if len(genres) > 1:
            fields["subgenre"] = ", ".join(genres[1:])
    if moods:
        fields["mood"] = moods[0]
        fields["moods"] = ", ".join(moods)
    if instruments:
        fields["instruments"] = ", ".join(instruments)
        if any("vocal" in item.lower() for item in instruments):
            fields["vocals"] = "vocal"
    if themes:
        fields["themes"] = ", ".join(themes)

    score_labels = [f"{_split_label(str(item.get('label', '')))}:{float(item.get('score') or 0):.3f}" for item in filtered[:8]]
    fields["analysis_summary"] = "Local AI audio tags: " + "; ".join(score_labels)
    fields["analysis_json"] = json.dumps({"local_ai_top_tags": filtered[:25]}, ensure_ascii=False)
    return fields


def _features_to_fields(features: dict[str, Any]) -> dict[str, str]:
    """Map algorithmic DSP descriptors (bpm/key/danceability) to tag fields."""
    if not features:
        return {}
    out: dict[str, str] = {}
    bpm = features.get("bpm")
    if isinstance(bpm, (int, float)) and bpm > 0:
        out["bpm"] = str(int(round(bpm)))
    key = features.get("key")
    if key:
        scale = features.get("scale") or ""
        out["key"] = f"{key} {scale}".strip()
    danceability = features.get("danceability")
    if isinstance(danceability, (int, float)):
        # Essentia danceability is ~0-3; normalize to a 0-1 field value.
        out["danceability"] = str(round(min(1.0, max(0.0, danceability / 3.0)), 3))
    if out:
        summary = ", ".join(f"{name}={value}" for name, value in out.items())
        out["analysis_summary"] = "Local DSP features: " + summary
    return out


class LocalAIAudioClient(ProviderClient):
    provider_name = "local_ai_audio"
    runner_name = ""

    def __init__(self, store, rate_limiter, settings: Settings) -> None:
        super().__init__(store, rate_limiter)
        self.settings = settings
        self.cache_ttl_seconds = settings.local_ai_cache_ttl_days * 24 * 60 * 60

    def model_paths(self) -> list[Path]:
        raise NotImplementedError

    def runner_args(self, media: MediaFile) -> list[str]:
        raise NotImplementedError

    def _raw_to_fields(self, raw: dict[str, Any]) -> dict[str, str]:
        fields = _field_map(raw.get("tags", []), self.settings.local_ai_min_score)
        for key, value in _features_to_fields(raw.get("features", {})).items():
            if value and key not in fields:
                fields[key] = value
        return fields

    def is_configured(self) -> bool:
        if not self.settings.local_ai_enabled:
            return False
        if importlib.util.find_spec("essentia") is None:
            return False
        return all(path.exists() and path.is_file() for path in self.model_paths())

    def _cache_key(self, media: MediaFile) -> str:
        payload = {
            "provider": self.provider_name,
            "path": str(media.path),
            "size_bytes": media.size_bytes,
            "mtime": media.mtime,
            "models": [str(path) for path in self.model_paths()],
            "top_n": self.settings.local_ai_top_n,
            "min_score": self.settings.local_ai_min_score,
        }
        return _sha256_payload(payload)

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        if not self.settings.local_ai_enabled:
            return None
        if not media.path.exists():
            return ProviderResult(self.provider_name, 0, {}, notes="media file not found")
        missing = [str(path) for path in self.model_paths() if not path.exists()]
        if missing:
            return ProviderResult(self.provider_name, 0, {}, notes=f"missing local model files: {', '.join(missing)}")
        if importlib.util.find_spec("essentia") is None:
            return ProviderResult(self.provider_name, 0, {}, notes="optional dependency not installed: essentia-tensorflow")

        cache_key = self._cache_key(media)
        cached = self.db.get_cache(self.provider_name, cache_key, self.cache_ttl_seconds)
        if cached is not None:
            fields = self._raw_to_fields(cached)
            return ProviderResult(self.provider_name, 0.88 if fields else 0.35, fields, notes="cached local audio analysis", raw=cached)

        command = [sys.executable, "-m", "tag_creator.local_ai_runner", *self.runner_args(media)]
        try:
            completed = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                timeout=self.settings.local_ai_timeout_seconds,
                check=False,
                env=thread_limited_env(self.settings),
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(self.provider_name, 0, {}, notes="local AI analysis timed out")

        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "local AI runner failed").strip()
            return ProviderResult(self.provider_name, 0, {}, notes=message[:500])

        try:
            raw = json.loads(completed.stdout)
        except ValueError:
            return ProviderResult(self.provider_name, 0, {}, notes="local AI runner returned invalid JSON")
        self.db.set_cache(self.provider_name, cache_key, 200, raw)
        fields = self._raw_to_fields(raw)
        return ProviderResult(
            self.provider_name,
            0.88 if fields else 0.35,
            fields,
            notes="local audio model analysis",
            raw=raw,
        )


class EssentiaFeaturesClient(LocalAIAudioClient):
    """Algorithmic BPM / musical key / danceability — Essentia only, no model files."""

    provider_name = "essentia_features"

    def model_paths(self) -> list[Path]:
        return []

    def runner_args(self, media: MediaFile) -> list[str]:
        return ["essentia_features", "--audio", str(media.path)]


class EssentiaDiscogsEffnetClient(LocalAIAudioClient):
    provider_name = "essentia_discogs_effnet"

    def model_paths(self) -> list[Path]:
        return [
            self.settings.essentia_discogs_embedding_model,
            self.settings.essentia_discogs_prediction_model,
            self.settings.essentia_discogs_labels,
        ]

    def runner_args(self, media: MediaFile) -> list[str]:
        args = [
            "essentia_discogs_effnet",
            "--audio",
            str(media.path),
            "--embedding-model",
            str(self.settings.essentia_discogs_embedding_model),
            "--prediction-model",
            str(self.settings.essentia_discogs_prediction_model),
            "--labels",
            str(self.settings.essentia_discogs_labels),
            "--top-n",
            str(self.settings.local_ai_top_n),
        ]
        # Extra mood/theme/instrument heads reuse the same Discogs-EffNet embedding.
        for head in self.settings.essentia_extra_heads:
            args.extend(["--head", head])
        return args


class MusicNNMtgJamendoClient(LocalAIAudioClient):
    provider_name = "musicnn_mtg_jamendo"

    def model_paths(self) -> list[Path]:
        return [
            self.settings.musicnn_mtg_jamendo_model,
            self.settings.musicnn_mtg_jamendo_labels,
        ]

    def runner_args(self, media: MediaFile) -> list[str]:
        return [
            "musicnn_mtg_jamendo",
            "--audio",
            str(media.path),
            "--prediction-model",
            str(self.settings.musicnn_mtg_jamendo_model),
            "--labels",
            str(self.settings.musicnn_mtg_jamendo_labels),
            "--top-n",
            str(self.settings.local_ai_top_n),
        ]
