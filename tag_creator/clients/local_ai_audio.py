from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import Settings
from ..models import MediaFile, ProviderResult
from ..resource_limits import thread_limited_env
from .base import ProviderClient


def _sha256_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Model taxonomies (from the Essentia model-zoo label files these providers use)
#
# The mapping below routes EVERY real prediction into the correct tag field:
#   * genre_discogs400 head  -> "Parent---Child" hierarchy (parent=genre, child=style)
#   * mtg_jamendo_moodtheme   -> mood / theme / occasion / season
#   * mtg_jamendo_instrument  -> instruments / vocals
#   * msd-musicnn (50 tags)   -> flat labels classified by the sets below
# Dedicated heads are trusted by their head name; the flat MSD model is classified
# by known-label membership. Nothing a model predicts is silently thrown away.
# ---------------------------------------------------------------------------

_VOCAL_LABELS = {
    "voice", "vocal", "vocals", "female vocalists", "male vocalists",
    "female vocalist", "male vocalist", "choir", "a cappella",
}

_INSTRUMENT_LABELS = {
    # mtg_jamendo_instrument head vocabulary
    "accordion", "acousticbassguitar", "acousticguitar", "bass", "beat", "bell",
    "bongo", "brass", "cello", "clarinet", "classicalguitar", "computer",
    "doublebass", "drummachine", "drums", "electricguitar", "electricpiano",
    "flute", "guitar", "harmonica", "harp", "horn", "keyboard", "orchestra",
    "organ", "pad", "percussion", "piano", "pipeorgan", "rhodes", "sampler",
    "saxophone", "strings", "synthesizer", "trombone", "trumpet", "viola",
    "violin",
    # MSD flat-tag instruments
    "acoustic", "instrumental",
    "electronic beat",
}

# Occasion / usage / setting -> themes (and a subset -> occasion / season)
_THEME_LABELS = {
    "action", "adventure", "advertising", "background", "children", "christmas",
    "commercial", "corporate", "documentary", "drama", "film", "game", "holiday",
    "movie", "nature", "party", "space", "sport", "summer", "trailer", "travel",
    "wedding",
}

# Affective descriptors -> mood / moods
_MOOD_LABELS = {
    # mtg_jamendo_moodtheme affective classes
    "ballad", "calm", "cool", "dark", "deep", "dramatic", "dream", "emotional",
    "energetic", "epic", "fast", "fun", "funny", "groovy", "happy", "heavy",
    "hopeful", "inspiring", "love", "meditative", "melancholic", "melodic",
    "motivational", "positive", "powerful", "relaxing", "retro", "romantic",
    "sad", "sexy", "slow", "soft", "soundscape", "upbeat", "uplifting",
    # MSD flat-tag moods
    "beautiful", "mellow", "chill", "chillout", "catchy", "easy listening",
}

# Flat MSD genre tags (Discogs head genres come through the "---" hierarchy path).
_GENRE_LABELS = {
    "rock", "pop", "alternative", "indie", "electronic", "dance", "alternative rock",
    "jazz", "metal", "classic rock", "soul", "indie rock", "electronica", "folk",
    "punk", "oldies", "blues", "hard rock", "ambient", "experimental", "hip-hop",
    "hip hop", "country", "funk", "electro", "heavy metal", "progressive rock",
    "rnb", "r&b", "indie pop", "house",
}

_OCCASION_LABELS = {
    "christmas", "holiday", "party", "wedding", "advertising", "commercial",
    "corporate", "film", "movie", "game", "trailer", "documentary", "sport",
}

_SEASON_LABELS = {"summer": "summer", "christmas": "winter"}

_ERA_RE = re.compile(r"^(00s|[6-9]0s|(?:19|20)\d0s)$")

# Run-together MTG instrument names -> readable form.
_READABLE = {
    "acousticguitar": "acoustic guitar",
    "electricguitar": "electric guitar",
    "classicalguitar": "classical guitar",
    "acousticbassguitar": "acoustic bass guitar",
    "electricpiano": "electric piano",
    "drummachine": "drum machine",
    "pipeorgan": "pipe organ",
    "doublebass": "double bass",
}

_VIDEO_LIKE_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm"}
_LOCAL_AI_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_LOCAL_AI_SEMAPHORES_LOCK = threading.Lock()


def _local_ai_semaphore(concurrency: int) -> threading.BoundedSemaphore:
    slots = max(1, concurrency)
    with _LOCAL_AI_SEMAPHORES_LOCK:
        semaphore = _LOCAL_AI_SEMAPHORES.get(slots)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(slots)
            _LOCAL_AI_SEMAPHORES[slots] = semaphore
        return semaphore


def _representative_offsets(duration_seconds: int | None, total_seconds: int, segments: int) -> list[tuple[int, int]]:
    """Return (start, duration) windows covering the audio without decoding all of it."""
    total_seconds = max(1, total_seconds)
    segments = max(1, segments)
    if not duration_seconds or duration_seconds <= total_seconds or segments == 1:
        return [(0, total_seconds)]

    segment_duration = max(1, total_seconds // segments)
    max_start = max(0, duration_seconds - segment_duration)
    if segments == 2:
        starts = [0, max_start]
    else:
        middle = max(0, int((duration_seconds - segment_duration) / 2))
        starts = [0, middle, max_start]
        if segments > 3:
            step = max(1, max_start // (segments - 1))
            starts = [min(max_start, index * step) for index in range(segments)]

    windows: list[tuple[int, int]] = []
    seen: set[int] = set()
    for start in starts:
        start = max(0, min(max_start, int(start)))
        if start in seen:
            continue
        seen.add(start)
        windows.append((start, segment_duration))
    return windows or [(0, total_seconds)]


def _pretty(label: str) -> str:
    """Readable tag: take the most specific hierarchy segment, tidy separators."""
    label = label.strip()
    for separator in ("---", "::", "|", "/"):
        if separator in label:
            label = label.split(separator)[-1]
    label = label.replace("_", " ").strip()
    return _READABLE.get(label.lower().replace(" ", ""), label)


def _categorize(raw: str, low: str, head: str) -> tuple[str, str, str]:
    """Return (category, value, extra) for one predicted label."""
    # 1) Discogs genre400 hierarchy "Parent---Child": parent=genre, child=style.
    if "---" in raw:
        parent, _, child = raw.partition("---")
        return ("genre_hierarchy", parent.strip(), _pretty(child))
    # 2) Trust dedicated prediction heads by name.
    if "instrument" in head:
        if low in _VOCAL_LABELS or low == "voice":
            return ("vocals", "vocal", "")
        return ("instrument", _pretty(raw), "")
    if "mood" in head or "theme" in head:
        return ("theme" if low in _THEME_LABELS else "mood", _pretty(raw), "")
    if "genre" in head:
        return ("genre", _pretty(raw), "")
    # 3) Flat label (MSD autotagger / unknown head): classify by known taxonomy.
    if low in _VOCAL_LABELS or low == "voice" or "vocal" in low:
        return ("vocals", "vocal", "")
    if low in _INSTRUMENT_LABELS:
        return ("instrument", _pretty(raw), "")
    if low in _THEME_LABELS:
        return ("theme", _pretty(raw), "")
    if low in _MOOD_LABELS:
        return ("mood", _pretty(raw), "")
    if low in _GENRE_LABELS:
        return ("genre", _pretty(raw), "")
    if _ERA_RE.match(low):
        return ("era", raw, "")
    # 4) Unknown flat label — keep as a genre-style descriptor rather than drop it.
    return ("genre", _pretty(raw), "")


def _field_map(tags: list[dict[str, Any]], min_score: float) -> dict[str, str]:
    """Map raw model predictions to tag fields using each model's OWN taxonomy.

    Predictions arrive ranked high->low score. Every prediction above ``min_score``
    is routed to the correct field by (1) the Discogs genre hierarchy, (2) the
    prediction head name, then (3) the known label taxonomies. The full ranked
    list is always retained in ``analysis_json`` so nothing is lost.
    """
    filtered = [tag for tag in tags if float(tag.get("score") or 0) >= min_score]
    if not filtered:
        return {}

    genres: list[str] = []
    styles: list[str] = []
    moods: list[str] = []
    themes: list[str] = []
    occasions: list[str] = []
    seasons: list[str] = []
    instruments: list[str] = []
    vocals: list[str] = []
    direct_fields: dict[str, str] = {}

    def _add(bucket: list[str], value: str) -> None:
        value = value.strip()
        if value and value.lower() not in {existing.lower() for existing in bucket}:
            bucket.append(value)

    for tag in filtered:
        raw = str(tag.get("label", "")).strip()
        if not raw:
            continue
        explicit_field = str(tag.get("field", "")).strip().lower()
        low = raw.lower()
        if explicit_field in {
            "energy",
            "valence",
            "danceability",
            "language",
            "occasion",
            "weather",
            "season",
            "age_group",
        }:
            direct_fields.setdefault(explicit_field, _pretty(raw))
            continue
        if explicit_field in {
            "genre",
            "subgenre",
            "mood",
            "moods",
            "instrument",
            "instruments",
            "vocals",
            "themes",
        }:
            category = {
                "subgenre": "style",
                "moods": "mood",
                "instrument": "instrument",
                "instruments": "instrument",
                "themes": "theme",
            }.get(explicit_field, explicit_field)
            value = _pretty(raw)
            extra = ""
        else:
            head = str(tag.get("head", "")).lower()
            category, value, extra = _categorize(raw, low, head)
        if category == "genre_hierarchy":
            _add(genres, value)
            if extra:
                _add(styles, extra)
        elif category == "style":
            _add(styles, value)
        elif category == "genre":
            _add(genres, value)
        elif category == "instrument":
            _add(instruments, value)
        elif category == "vocals":
            _add(vocals, "instrumental" if value.lower() == "instrumental" else "vocal")
        elif category == "mood":
            _add(moods, value)
        elif category == "theme":
            _add(themes, value)
            if low in _OCCASION_LABELS:
                _add(occasions, value)
            if low in _SEASON_LABELS:
                _add(seasons, _SEASON_LABELS[low])
        # 'era' stays only in analysis_json below — retained, never silently dropped.

    fields: dict[str, str] = {}
    if genres:
        fields["genre"] = genres[0]
        extra_styles = styles + genres[1:]
        if extra_styles:
            fields["subgenre"] = ", ".join(extra_styles[:4])
    if moods:
        fields["mood"] = moods[0]
        fields["moods"] = ", ".join(moods[:6])
    if themes:
        fields["themes"] = ", ".join(themes[:6])
    if occasions:
        fields["occasion"] = ", ".join(occasions[:3])
    if seasons:
        fields["season"] = seasons[0]
    if instruments:
        fields["instruments"] = ", ".join(instruments[:8])
    if vocals:
        fields["vocals"] = vocals[0]
    for key, value in direct_fields.items():
        fields.setdefault(key, value)

    # Provenance: the full ranked prediction list is always retained.
    score_labels = [
        f"{_pretty(str(tag.get('label', '')))}:{float(tag.get('score') or 0):.3f}"
        for tag in filtered[:10]
    ]
    fields["analysis_summary"] = "Local AI audio tags: " + "; ".join(score_labels)
    fields["analysis_json"] = json.dumps({"local_ai_top_tags": filtered[:40]}, ensure_ascii=False)
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

    @staticmethod
    def _evidence_confidence(raw: dict[str, Any], fields: dict[str, str]) -> float:
        substantive = {key: value for key, value in fields.items() if key not in {"analysis_summary", "analysis_json"} and value}
        if not substantive:
            return 0.35

        confidence = 0.55
        features = raw.get("features") or {}
        if any(features.get(key) not in {None, ""} for key in ("bpm", "key", "danceability")):
            # Deterministic DSP measurements are stronger evidence than a broad
            # zero-shot label, while still remaining estimates from the audio.
            confidence = 0.86

        scores = [
            max(0.0, min(1.0, float(tag.get("score") or 0)))
            for tag in (raw.get("tags") or [])
            if tag.get("label")
        ]
        if scores:
            top = max(scores)
            confidence = max(confidence, min(0.92, 0.55 + (top * 0.42)))
        return round(confidence, 3)

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
            "preview_seconds": self.settings.local_ai_audio_preview_seconds,
            "preview_segments": self.settings.local_ai_audio_preview_segments,
        }
        return _sha256_payload(payload)

    @contextmanager
    def _analysis_audio_path(self, media: MediaFile) -> Iterator[Path]:
        """Yield a bounded audio-only file for video containers.

        MP4 files can be large, malformed, or contain video streams that make
        downstream audio loaders slow. Local AI only needs representative audio,
        so extract a deterministic preview window with ffmpeg and keep the
        original media path for CSV identity/resume.
        """
        if media.extension.lower() not in _VIDEO_LIKE_EXTENSIONS:
            yield media.path
            return

        with tempfile.TemporaryDirectory(prefix="tag_creator_audio_") as tmpdir:
            preview = Path(tmpdir) / f"{media.path.stem}.wav"
            windows = _representative_offsets(
                media.duration_seconds,
                self.settings.local_ai_audio_preview_seconds,
                self.settings.local_ai_audio_preview_segments,
            )
            filter_parts: list[str] = []
            concat_inputs: list[str] = []
            for index, (start, duration) in enumerate(windows):
                label = f"a{index}"
                filter_parts.append(
                    f"[0:a]atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS[{label}]"
                )
                concat_inputs.append(f"[{label}]")
            filter_complex = ";".join(filter_parts + [f"{''.join(concat_inputs)}concat=n={len(windows)}:v=0:a=1[outa]"])
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media.path),
                "-filter_complex",
                filter_complex,
                "-map",
                "[outa]",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                str(preview),
            ]
            completed = self._run_ffmpeg_preview(command)
            if completed.returncode != 0 or not preview.exists() or preview.stat().st_size == 0:
                # Some MP4 files have stream layouts that reject filter_complex.
                # Fall back to a single bounded window so the file still gets real
                # audio analysis instead of failing the whole local-AI stage.
                fallback_start, fallback_duration = windows[len(windows) // 2]
                fallback = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    str(fallback_start),
                    "-i",
                    str(media.path),
                    "-t",
                    str(fallback_duration),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    str(preview),
                ]
                completed = self._run_ffmpeg_preview(fallback)
                if completed.returncode != 0 or not preview.exists() or preview.stat().st_size == 0:
                    message = (completed.stderr or completed.stdout or "ffmpeg audio preview failed").strip()
                    raise RuntimeError(message[:500])
            yield preview

    def _run_ffmpeg_preview(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.settings.local_ai_audio_preview_timeout_seconds,
                check=False,
                env=thread_limited_env(self.settings),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"ffmpeg audio preview timed out after "
                f"{self.settings.local_ai_audio_preview_timeout_seconds}s"
            ) from exc

    def _analyze(self, media: MediaFile) -> dict[str, Any]:
        with _local_ai_semaphore(self.settings.local_ai_concurrency):
            with self._analysis_audio_path(media) as audio_path:
                analysis_media = media if audio_path == media.path else MediaFile(
                    path=audio_path,
                    extension=audio_path.suffix.lower(),
                    size_bytes=audio_path.stat().st_size,
                    mtime=audio_path.stat().st_mtime,
                    duration_seconds=media.duration_seconds,
                    bitrate=media.bitrate,
                    tags=media.tags,
                    has_cover_art=media.has_cover_art,
                )
                command = [sys.executable, "-m", "tag_creator.local_ai_runner", *self.runner_args(analysis_media)]
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
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError("local AI analysis timed out") from exc

        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "local AI runner failed").strip()
            raise RuntimeError(message[:500])
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            raise RuntimeError("local AI runner returned invalid JSON") from exc

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
            confidence = self._evidence_confidence(cached, fields)
            return ProviderResult(self.provider_name, confidence, fields, notes="cached local audio analysis", raw=cached)

        try:
            raw = self._analyze(media)
        except (TimeoutError, RuntimeError) as exc:
            return ProviderResult(self.provider_name, 0, {}, notes=str(exc))
        except Exception as exc:  # noqa: BLE001 - isolate optional model failures per provider
            return ProviderResult(
                self.provider_name,
                0,
                {},
                notes=f"local AI analysis failed: {type(exc).__name__}: {str(exc)[:400]}",
            )
        self.db.set_cache(self.provider_name, cache_key, 200, raw)
        fields = self._raw_to_fields(raw)
        confidence = self._evidence_confidence(raw, fields)
        return ProviderResult(
            self.provider_name,
            confidence,
            fields,
            notes=f"local audio model analysis; evidence_confidence={confidence:.3f}",
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


class ClapZeroShotClient(LocalAIAudioClient):
    """Heavier local AI descriptor layer using CLAP zero-shot audio/text matching."""

    provider_name = "clap_zero_shot"

    def __init__(self, store, rate_limiter, settings: Settings) -> None:
        super().__init__(store, rate_limiter, settings)
        self._inference_slots = threading.BoundedSemaphore(settings.clap_concurrency)

    def model_paths(self) -> list[Path]:
        # Hugging Face files are managed in clap_cache_dir, not fixed .pb files.
        return []

    def is_configured(self) -> bool:
        if not self.settings.local_ai_enabled:
            return False
        if not self.settings.clap_label_specs:
            return False
        return (
            importlib.util.find_spec("essentia") is not None
            and importlib.util.find_spec("transformers") is not None
            and importlib.util.find_spec("torch") is not None
        )

    def _cache_key(self, media: MediaFile) -> str:
        payload = {
            "provider": self.provider_name,
            "path": str(media.path),
            "size_bytes": media.size_bytes,
            "mtime": media.mtime,
            "model_name": self.settings.clap_model_name,
            "label_specs": self.settings.clap_label_specs,
            "top_n": self.settings.local_ai_top_n,
            "min_score": self.settings.local_ai_min_score,
            "max_seconds": self.settings.clap_max_seconds,
        }
        return _sha256_payload(payload)

    def _analyze(self, media: MediaFile) -> dict[str, Any]:
        if self.settings.clap_subprocess:
            return super()._analyze(media)

        # CLAP stays in this process so its model and text embeddings are loaded
        # once and reused across the entire library. The semaphore bounds memory
        # and CPU use while other file workers continue API/web stages.
        from argparse import Namespace

        from ..local_ai_runner import run_clap_zero_shot

        args = Namespace(
            audio=media.path,
            model_name=self.settings.clap_model_name,
            cache_dir=self.settings.clap_cache_dir,
            label=self.settings.clap_label_specs,
            top_n=self.settings.local_ai_top_n,
            max_seconds=self.settings.clap_max_seconds,
        )
        with self._inference_slots:
            return run_clap_zero_shot(args)

    def runner_args(self, media: MediaFile) -> list[str]:
        args = [
            "clap_zero_shot",
            "--audio",
            str(media.path),
            "--model-name",
            self.settings.clap_model_name,
            "--cache-dir",
            str(self.settings.clap_cache_dir),
            "--top-n",
            str(self.settings.local_ai_top_n),
        ]
        for spec in self.settings.clap_label_specs:
            args.extend(["--label", spec])
        return args
