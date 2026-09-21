from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from tqdm import tqdm

LOGGER = logging.getLogger(__name__)

DEFAULT_MP3_NORMALIZATION_DIR_NAME = "normalization-mp3"
DEFAULT_MP4_NORMALIZATION_DIR_NAME = "normalization-mp4"
LEGACY_NORMALIZATION_DIR_NAME = "normalization"
MP3GAIN_REFERENCE_DB = 89.0
EXCLUDED_NORMALIZATION_DIR_NAMES = {
    "normalized",
    LEGACY_NORMALIZATION_DIR_NAME,
    DEFAULT_MP3_NORMALIZATION_DIR_NAME,
    DEFAULT_MP4_NORMALIZATION_DIR_NAME,
}


def normalization_enabled() -> bool:
    return os.getenv("MEDIA_NORMALIZATION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _bool_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def target_lufs_from_db(target_db: float) -> float:
    # ReplayGain/MP3Gain's 89 dB reference corresponds approximately to -18 LUFS.
    return target_db - 107.0


def _mp4_target_lufs_from_env() -> float:
    configured = os.getenv("MEDIA_NORMALIZATION_MP4_TARGET_LUFS", "").strip()
    if configured:
        try:
            return float(configured)
        except ValueError:
            pass
    return target_lufs_from_db(_float_from_env("MEDIA_NORMALIZATION_TARGET_DB", 89.0))


def _float_from_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _normalization_dir_name(env_name: str, default: str) -> str:
    legacy = os.getenv("MEDIA_NORMALIZATION_DIR_NAME", "").strip()
    return os.getenv(env_name, "").strip() or legacy or default


def _normalization_output_scope() -> str:
    return os.getenv("MEDIA_NORMALIZATION_OUTPUT_SCOPE", "input-root").strip().lower().replace("_", "-")


def _list_from_env(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {
        value.strip().casefold()
        for chunk in raw.split(";")
        for value in chunk.split(",")
        if value.strip()
    }


def _should_flatten_input_root(input_dir: Path) -> bool:
    roots = _list_from_env("MEDIA_NORMALIZATION_FLATTEN_ROOTS")
    return "*" in roots or input_dir.name.casefold() in roots


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return cleaned or "folder"


def _normalization_target_for(source: Path, input_dir: Path, env_name: str, default: str) -> Path:
    directory_name = _normalization_dir_name(env_name, default)
    if _should_flatten_input_root(input_dir):
        return input_dir / directory_name / source.name
    scope = _normalization_output_scope()
    if scope in {"media-folder", "media-parent", "next-to-source", "source-parent"}:
        return source.parent / directory_name / source.name
    try:
        relative = source.relative_to(input_dir)
    except ValueError:
        relative = Path(source.name)
    return input_dir / directory_name / relative


def _dedupe_target_path(source: Path, input_dir: Path, target: Path, used_targets: dict[str, Path]) -> Path:
    key = str(target).casefold()
    existing_source = used_targets.get(key)
    if existing_source is None or existing_source == source:
        used_targets[key] = source
        return target

    try:
        relative_parent = source.relative_to(input_dir).parent
    except ValueError:
        relative_parent = Path()
    prefix = " - ".join(_safe_filename_part(part) for part in relative_parent.parts)
    candidate_name = f"{prefix} - {source.name}" if prefix else source.name
    candidate = target.with_name(candidate_name)
    counter = 2
    while str(candidate).casefold() in used_targets and used_targets[str(candidate).casefold()] != source:
        candidate = target.with_name(f"{Path(candidate_name).stem} ({counter}){source.suffix}")
        counter += 1
    used_targets[str(candidate).casefold()] = source
    return candidate


def _cleanup_legacy_per_folder_normalization_dirs(input_dir: Path, dirname: str) -> int:
    if not _should_flatten_input_root(input_dir):
        return 0
    root_output = (input_dir / dirname).resolve()
    removed = 0
    for candidate in sorted(input_dir.rglob(dirname), reverse=True):
        if not candidate.is_dir():
            continue
        try:
            if candidate.resolve() == root_output:
                continue
        except OSError:
            continue
        try:
            shutil.rmtree(candidate)
            removed += 1
        except OSError as exc:
            LOGGER.warning("could not remove legacy normalization folder %s: %s", candidate, exc)
    if removed:
        LOGGER.info("removed legacy per-folder %s directories under %s: %s", dirname, input_dir, removed)
    return removed


def _cleanup_normalization_sidecars(input_dir: Path) -> int:
    removed = 0
    for sidecar in input_dir.rglob("*.normalization.json"):
        if not sidecar.is_file():
            continue
        try:
            sidecar.unlink()
            removed += 1
        except OSError as exc:
            LOGGER.warning("could not remove normalization sidecar %s: %s", sidecar, exc)
    if removed:
        LOGGER.info("removed normalization JSON sidecar files under %s: %s", input_dir, removed)
    return removed


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


def _mp4_files(input_dir: Path) -> list[Path]:
    files = [
        path
        for path in input_dir.rglob("*.mp4")
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


def _ffprobe_path() -> str:
    return os.getenv("FFPROBE_PATH", "ffprobe").strip() or "ffprobe"


def _mp3gain_path() -> str:
    configured = os.getenv("MP3GAIN_PATH", "mp3gain").strip() or "mp3gain"
    configured_path = Path(configured)
    if configured_path.is_file():
        return str(configured_path)
    resolved = shutil.which(configured)
    return resolved or ""


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


def _normalize_mp3_with_mp3gain(source: Path, target: Path, target_db: float) -> bool:
    mp3gain = _mp3gain_path()
    if not mp3gain:
        LOGGER.warning(
            "mp3gain is required for exact 89 dB MP3 normalization but was not found. "
            "Install mp3gain in the Docker image or set MP3GAIN_PATH."
        )
        return False

    temporary_target = target.with_name(f".{target.stem}.tmp{target.suffix}")
    temporary_target.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary_target)
    except OSError as exc:
        LOGGER.warning("mp3gain normalization could not copy %s: %s", source, exc)
        return False

    target_delta = target_db - MP3GAIN_REFERENCE_DB
    avoid_clipping = os.getenv("MEDIA_NORMALIZATION_MP3_AVOID_CLIPPING", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    command = [
        mp3gain,
        "-r",
        "-c",
        "-s",
        "i",
        "-d",
        f"{target_delta:.1f}",
    ]
    if avoid_clipping:
        command.append("-k")
    command.append(str(temporary_target))

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=360, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        temporary_target.unlink(missing_ok=True)
        LOGGER.warning("mp3gain normalization could not run for %s: %s", source, exc)
        return False
    if completed.returncode != 0 or not temporary_target.exists() or temporary_target.stat().st_size == 0:
        temporary_target.unlink(missing_ok=True)
        LOGGER.warning("mp3gain normalization failed for %s: %s", source, (completed.stderr or completed.stdout or "").strip())
        return False

    temporary_target.replace(target)
    shutil.copystat(source, target)
    return True


def _repair_mp3_for_mp3gain(source: Path, target: Path) -> Path | None:
    bitrate = os.getenv("MEDIA_NORMALIZATION_MP3_BITRATE", "320k").strip() or "320k"
    repaired = target.with_name(f".{target.stem}.repair{target.suffix}")
    repaired.unlink(missing_ok=True)
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
        "-codec:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-map_metadata",
        "0",
        "-f",
        "mp3",
        str(repaired),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=360, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        repaired.unlink(missing_ok=True)
        LOGGER.warning("mp3 repair could not run for %s: %s", source, exc)
        return None
    if completed.returncode != 0 or not repaired.exists() or repaired.stat().st_size == 0:
        repaired.unlink(missing_ok=True)
        LOGGER.warning("mp3 repair failed for %s: %s", source, (completed.stderr or completed.stdout or "").strip())
        return None
    return repaired


def _normalize_mp3_with_ffmpeg_loudnorm(source: Path, target: Path, target_db: float) -> bool:
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


def normalize_mp3_file(source: Path, target: Path) -> bool:
    target_db = _float_from_env("MEDIA_NORMALIZATION_TARGET_DB", 89.0)
    mode = os.getenv("MEDIA_NORMALIZATION_MP3_MODE", "mp3gain").strip().lower()
    if mode in {"mp3gain", "mp3_gain", "replaygain", "replay_gain"}:
        if _normalize_mp3_with_mp3gain(source, target, target_db):
            return True
        if _bool_from_env("MEDIA_NORMALIZATION_MP3_REPAIR_WITH_FFMPEG", True):
            LOGGER.warning(
                "mp3gain failed for %s; repairing MP3 with FFmpeg and retrying mp3gain.",
                source,
            )
            repaired = _repair_mp3_for_mp3gain(source, target)
            if repaired is not None:
                try:
                    if _normalize_mp3_with_mp3gain(repaired, target, target_db):
                        shutil.copystat(source, target)
                        return True
                finally:
                    repaired.unlink(missing_ok=True)
        fallback = os.getenv("MEDIA_NORMALIZATION_MP3_FALLBACK", "ffmpeg").strip().lower()
        if fallback in {"1", "true", "yes", "on", "ffmpeg", "loudnorm", "ffmpeg_loudnorm"}:
            LOGGER.warning(
                "mp3gain failed for %s; falling back to FFmpeg loudnorm. "
                "This creates a normalized MP3, but exact MP3Gain 89 dB requires mp3gain.",
                source,
            )
            return _normalize_mp3_with_ffmpeg_loudnorm(source, target, target_db)
        return False
    if mode in {"ffmpeg", "loudnorm", "ffmpeg_loudnorm"}:
        LOGGER.warning(
            "MEDIA_NORMALIZATION_MP3_MODE=%s uses LUFS, not MP3Gain 89 dB. "
            "Use MEDIA_NORMALIZATION_MP3_MODE=mp3gain for exact MP3Gain-style target.",
            mode,
        )
        return _normalize_mp3_with_ffmpeg_loudnorm(source, target, target_db)
    LOGGER.warning("unknown MEDIA_NORMALIZATION_MP3_MODE=%s; expected mp3gain or ffmpeg_loudnorm", mode)
    return False


def _mp4_normalization_enabled() -> bool:
    return os.getenv("MEDIA_NORMALIZATION_MP4_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _has_audio_stream(source: Path) -> bool:
    command = [
        _ffprobe_path(),
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(source),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("mp4 normalization probe could not run for %s: %s", source, exc)
        return False
    if completed.returncode != 0:
        LOGGER.warning("mp4 normalization probe failed for %s: %s", source, (completed.stderr or "").strip())
        return False
    return bool((completed.stdout or "").strip())


def normalize_mp4_file(source: Path, target: Path) -> bool:
    if not _has_audio_stream(source):
        LOGGER.info("mp4 normalization skipped no-audio file: %s", source)
        return False

    target_lufs = _mp4_target_lufs_from_env()
    true_peak = _float_from_env("MEDIA_NORMALIZATION_MP4_TRUE_PEAK", -1.0)
    lra = _float_from_env("MEDIA_NORMALIZATION_MP4_LRA", 11.0)
    bitrate = os.getenv("MEDIA_NORMALIZATION_MP4_AUDIO_BITRATE", "256k").strip() or "256k"
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
        "0:v?",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        "-ar",
        "48000",
        "-filter:a",
        f"loudnorm=I={target_lufs:.1f}:TP={true_peak:.1f}:LRA={lra:.1f}",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary_target),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        temporary_target.unlink(missing_ok=True)
        LOGGER.warning("mp4 normalization could not run for %s: %s", source, exc)
        return False
    if completed.returncode != 0 or not temporary_target.exists() or temporary_target.stat().st_size == 0:
        temporary_target.unlink(missing_ok=True)
        LOGGER.warning("mp4 normalization failed for %s: %s", source, (completed.stderr or completed.stdout or "").strip())
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
        LOGGER.info("mp3 normalization: no mp3 files found in %s", input_dir)
        return 0, 0

    LOGGER.info("mp3 normalization: found %s file(s) in %s", len(files), input_dir)
    mp3_dirname = _normalization_dir_name("MEDIA_NORMALIZATION_MP3_DIR_NAME", DEFAULT_MP3_NORMALIZATION_DIR_NAME)
    _cleanup_legacy_per_folder_normalization_dirs(input_dir, mp3_dirname)
    created = 0
    skipped = 0
    used_targets: dict[str, Path] = {}
    for source in tqdm(files, desc="Normalizing MP3", unit="file", dynamic_ncols=True):
        target = _normalization_target_for(
            source,
            input_dir,
            "MEDIA_NORMALIZATION_MP3_DIR_NAME",
            DEFAULT_MP3_NORMALIZATION_DIR_NAME,
        )
        target = _dedupe_target_path(source, input_dir, target, used_targets)
        target_dir = target.parent
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


def normalize_mp4_directories(input_dir: Path) -> tuple[int, int]:
    if not normalization_enabled() or not _mp4_normalization_enabled():
        return 0, 0
    if not input_dir.exists():
        return 0, 0

    files = _mp4_files(input_dir)
    if not files:
        LOGGER.info("mp4 normalization: no mp4 files found in %s", input_dir)
        return 0, 0

    LOGGER.info("mp4 normalization: found %s file(s) in %s", len(files), input_dir)
    mp4_dirname = _normalization_dir_name("MEDIA_NORMALIZATION_MP4_DIR_NAME", DEFAULT_MP4_NORMALIZATION_DIR_NAME)
    _cleanup_legacy_per_folder_normalization_dirs(input_dir, mp4_dirname)
    created = 0
    skipped = 0
    used_targets: dict[str, Path] = {}
    for source in tqdm(files, desc="Normalizing MP4", unit="file", dynamic_ncols=True):
        target = _normalization_target_for(
            source,
            input_dir,
            "MEDIA_NORMALIZATION_MP4_DIR_NAME",
            DEFAULT_MP4_NORMALIZATION_DIR_NAME,
        )
        target = _dedupe_target_path(source, input_dir, target, used_targets)
        target_dir = target.parent
        if not _needs_update(source, target):
            skipped += 1
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.warning("mp4 normalization folder cannot be created for %s: %s", source.parent, exc)
            skipped += 1
            continue
        if normalize_mp4_file(source, target):
            created += 1
        else:
            skipped += 1

    if created or skipped:
        LOGGER.info("mp4 normalization complete: normalized=%s skipped=%s input=%s", created, skipped, input_dir)
    return created, skipped


def normalize_media_directories(input_dir: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    LOGGER.info("media normalization starting: %s", input_dir)
    _cleanup_normalization_sidecars(input_dir)
    mp3_stats = normalize_mp3_directories(input_dir)
    mp4_stats = normalize_mp4_directories(input_dir)
    LOGGER.info(
        "media normalization finished: mp3 normalized=%s skipped=%s; mp4 normalized=%s skipped=%s",
        mp3_stats[0],
        mp3_stats[1],
        mp4_stats[0],
        mp4_stats[1],
    )
    return mp3_stats, mp4_stats
