from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_NORMALIZATION_DIR_NAME = "normalization"
EXCLUDED_NORMALIZATION_DIR_NAMES = {"normalized", DEFAULT_NORMALIZATION_DIR_NAME}


def normalization_enabled() -> bool:
    return os.getenv("MEDIA_NORMALIZATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def target_lufs_from_db(target_db: float) -> float:
    # ReplayGain/MP3Gain's 89 dB reference corresponds approximately to -18 LUFS.
    return target_db - 107.0


def _float_from_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _normalization_dir_for(path: Path) -> Path:
    return path.parent / os.getenv("MEDIA_NORMALIZATION_DIR_NAME", DEFAULT_NORMALIZATION_DIR_NAME).strip()


def _is_inside_excluded_dir(path: Path, input_dir: Path) -> bool:
    try:
        relative_parts = path.relative_to(input_dir).parts[:-1]
    except ValueError:
        relative_parts = path.parts[:-1]
    excluded = {
        value.strip().casefold()
        for value in os.getenv(
            "EXCLUDED_MEDIA_DIR_NAMES",
            ",".join(sorted(EXCLUDED_NORMALIZATION_DIR_NAMES)),
        ).split(",")
        if value.strip()
    } | EXCLUDED_NORMALIZATION_DIR_NAMES
    return any(part.casefold() in excluded for part in relative_parts)


def _mp3_files(input_dir: Path) -> list[Path]:
    files = [
        path
        for path in input_dir.rglob("*.mp3")
        if path.is_file() and not _is_inside_excluded_dir(path, input_dir)
    ]
    files.sort()
    return files


def _needs_update(source: Path, target: Path) -> bool:
    if not target.exists() or target.stat().st_size == 0:
        return True
    return source.stat().st_mtime > target.stat().st_mtime


def _ffmpeg_path() -> str:
    return os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"


def _loudnorm_measure(source: Path, target_lufs: float, true_peak: float, lra: float) -> dict[str, str] | None:
    command = [
        _ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-af",
        f"loudnorm=I={target_lufs:.1f}:TP={true_peak:.1f}:LRA={lra:.1f}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("normalization measurement could not run for %s: %s", source, exc)
        return None
    if completed.returncode != 0:
        LOGGER.warning("normalization measurement failed for %s: %s", source, (completed.stderr or "").strip())
        return None
    match = re.search(r"\{[\s\S]*\}", completed.stderr or completed.stdout or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    required = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    return {key: str(data.get(key, "")) for key in required if str(data.get(key, "")).strip()}


def normalize_mp3_file(source: Path, target: Path) -> bool:
    target_db = _float_from_env("MEDIA_NORMALIZATION_TARGET_DB", 89.0)
    target_lufs = target_lufs_from_db(target_db)
    true_peak = _float_from_env("MEDIA_NORMALIZATION_TRUE_PEAK", -1.5)
    lra = _float_from_env("MEDIA_NORMALIZATION_LRA", 11.0)
    bitrate = os.getenv("MEDIA_NORMALIZATION_MP3_BITRATE", "320k").strip() or "320k"
    measured = _loudnorm_measure(source, target_lufs, true_peak, lra)
    filter_args = f"loudnorm=I={target_lufs:.1f}:TP={true_peak:.1f}:LRA={lra:.1f}"
    if measured:
        filter_args = (
            f"{filter_args}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true:print_format=summary"
        )

    temporary_target = target.with_name(f".{target.stem}.tmp{target.suffix}")
    command = [
        _ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filter_args,
        "-codec:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-map_metadata",
        "0",
        "-f",
        "mp3",
        str(temporary_target),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=360, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        temporary_target.unlink(missing_ok=True)
        LOGGER.warning("normalization could not run for %s: %s", source, exc)
        return False
    if completed.returncode != 0 or not temporary_target.exists() or temporary_target.stat().st_size == 0:
        temporary_target.unlink(missing_ok=True)
        LOGGER.warning("normalization failed for %s: %s", source, (completed.stderr or completed.stdout or "").strip())
        return False
    temporary_target.replace(target)
    shutil.copystat(source, target)
    return True


def normalize_mp3_directories(input_dir: Path) -> tuple[int, int]:
    if not normalization_enabled():
        return 0, 0
    if not input_dir.exists():
        return 0, 0

    files = _mp3_files(input_dir)
    if not files:
        return 0, 0

    created = 0
    skipped = 0
    for source in files:
        target_dir = _normalization_dir_for(source)
        target = target_dir / source.name
        if not _needs_update(source, target):
            skipped += 1
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.warning("normalization folder cannot be created for %s: %s", source.parent, exc)
            skipped += 1
            continue
        if normalize_mp3_file(source, target):
            created += 1
        else:
            skipped += 1

    if created or skipped:
        LOGGER.info("mp3 normalization complete: normalized=%s skipped=%s input=%s", created, skipped, input_dir)
    return created, skipped
