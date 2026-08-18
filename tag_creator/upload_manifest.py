from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tag_creator.media import read_media_file


DEFAULT_MEDIA_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".aac", ".flac", ".wav", ".wma", ".ogg"}
DEFAULT_EXCLUDED_DIR_NAMES = {"normalized"}
MANIFEST_SCHEMA_VERSION = 1

_VIDEO_NOISE_PATTERNS = (
    r"official\s+music\s+video",
    r"official\s+lyric\s+video",
    r"official\s+lyrics\s+video",
    r"official\s+performance\s+video",
    r"official\s+visuali[sz]er",
    r"official\s+audio",
    r"official\s+video",
    r"music\s+video",
    r"lyric\s+video",
    r"lyrics\s+video",
    r"performance\s+video",
    r"visuali[sz]er",
)
_BRACKETED_NOISE = re.compile(
    r"\s*[\[(]\s*(?:" + "|".join(_VIDEO_NOISE_PATTERNS) + r")\s*[\])]\s*",
    flags=re.IGNORECASE,
)
_TRAILING_NOISE = re.compile(
    r"(?:\s+[-_]\s+|\s+)(?:" + "|".join(_VIDEO_NOISE_PATTERNS) + r")\s*$",
    flags=re.IGNORECASE,
)
_GENERIC_STEM = re.compile(
    r"(?:\d{4,}|[a-f0-9]{8,}|track\s*\d+|audio\s*\d+|video\s*\d+|file\s*\d+|download\s*\d+)$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class PreparedFile:
    source_path: Path
    target_path: Path
    status: str
    reason: str = ""


@dataclass(frozen=True)
class PrepareSummary:
    scanned: int
    known: int
    marked_existing: int
    planned: int
    renamed: int
    copied: int
    unchanged: int
    manifest_path: Path
    dry_run: bool
    files: list[PreparedFile]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: object) -> str:
    return unicodedata.normalize("NFC", " ".join(str(value or "").strip().split()))


def parse_path_list(value: str) -> list[str]:
    values: list[str] = []
    for semicolon_part in value.split(";"):
        values.extend(part.strip() for part in semicolon_part.split(",") if part.strip())
    return values


def parse_extensions(value: str) -> set[str]:
    extensions = {
        extension.strip().lower()
        for extension in value.split(",")
        if extension.strip()
    }
    return {extension if extension.startswith(".") else f".{extension}" for extension in extensions}


def parse_excluded_dir_names(value: str) -> set[str]:
    names = {part.strip().casefold() for part in value.split(",") if part.strip()}
    return names or set(DEFAULT_EXCLUDED_DIR_NAMES)


def load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "files": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "files": {}}
    if not isinstance(loaded, dict):
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "files": {}}
    if not isinstance(loaded.get("files"), dict):
        loaded["files"] = {}
    loaded["schema_version"] = MANIFEST_SCHEMA_VERSION
    return loaded


def save_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["updated_at"] = utc_now()
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def relative_key(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return relative.as_posix()


def manifest_key(root: Path, path: Path) -> str:
    return f"{root.resolve().as_posix()}::{relative_key(root, path).casefold()}"


def file_state(root: Path, path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "root": root.resolve().as_posix(),
        "current_relpath": relative_key(root, path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def is_known_file(root: Path, path: Path, manifest: dict[str, object]) -> bool:
    files = manifest.get("files", {})
    if not isinstance(files, dict):
        return False
    entry = files.get(manifest_key(root, path))
    if not isinstance(entry, dict):
        return False
    state = file_state(root, path)
    return (
        entry.get("current_relpath") == state["current_relpath"]
        and entry.get("size_bytes") == state["size_bytes"]
        and entry.get("mtime_ns") == state["mtime_ns"]
    )


def record_file(
    manifest: dict[str, object],
    root: Path,
    source_path: Path,
    target_path: Path,
    *,
    status: str,
    output_path: Path | None = None,
) -> None:
    files = manifest.setdefault("files", {})
    if not isinstance(files, dict):
        manifest["files"] = files = {}

    state = file_state(root, target_path)
    now = utc_now()
    key = manifest_key(root, target_path)
    source_relpath = relative_key(root, source_path)
    existing = files.get(key) if isinstance(files.get(key), dict) else {}
    files[key] = {
        **state,
        "original_relpath": existing.get("original_relpath", source_relpath),
        "first_seen_at": existing.get("first_seen_at", now),
        "last_seen_at": now,
        "status": status,
    }
    if output_path:
        files[key]["output_path"] = output_path.resolve().as_posix()
    old_key = manifest_key(root, source_path)
    if old_key != key:
        files.pop(old_key, None)


def cleaned_media_filename(filename: str) -> str:
    path = Path(clean_text(filename))
    suffix = path.suffix.lower()
    stem = clean_text(path.stem)
    stem = re.sub(r"\s*[\u2010-\u2015]\s*", " - ", stem)
    previous = ""
    while previous != stem:
        previous = stem
        stem = _BRACKETED_NOISE.sub(" ", stem)
        stem = _TRAILING_NOISE.sub("", stem)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", stem)
    stem = re.sub(r"\s+-\s+", " - ", stem)
    stem = clean_text(stem).strip(" .-_")
    return f"{stem or path.stem or 'media'}{suffix}"


def is_generic_media_stem(stem: str) -> bool:
    cleaned = clean_text(stem).strip(" .-_")
    if not cleaned:
        return True
    if re.search(r"\s+-\s+", cleaned):
        return False
    return bool(_GENERIC_STEM.fullmatch(cleaned))


def safe_filename_component(value: str) -> str:
    cleaned = clean_text(value)
    cleaned = _BRACKETED_NOISE.sub(" ", cleaned)
    cleaned = _TRAILING_NOISE.sub("", cleaned)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", cleaned)
    cleaned = re.sub(r"\s+-\s+", " - ", cleaned)
    return clean_text(cleaned).strip(" .-_")


def embedded_media_filename(path: Path) -> str:
    if not is_generic_media_stem(path.stem):
        return ""
    try:
        media = read_media_file(path)
    except Exception:
        return ""
    artist = safe_filename_component(media.tags.get("artist", ""))
    title = safe_filename_component(media.tags.get("title", ""))
    if not artist or not title:
        return ""
    if artist.casefold() == title.casefold():
        return ""
    return f"{artist} - {title}{path.suffix.lower()}"


def prepared_media_filename(path: Path) -> str:
    return embedded_media_filename(path) or cleaned_media_filename(path.name)


def unique_target_path(target_path: Path) -> Path:
    if not target_path.exists():
        return target_path
    counter = 2
    while True:
        candidate = target_path.with_name(f"{target_path.stem} ({counter}){target_path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def scan_media_files(
    roots: list[Path],
    *,
    extensions: set[str],
    excluded_dir_names: set[str],
) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            relative_parts = path.relative_to(root).parts[:-1]
            if any(part.casefold() in excluded_dir_names for part in relative_parts):
                continue
            files.append((root, path))
    files.sort(key=lambda item: (item[0].as_posix().casefold(), relative_key(item[0], item[1]).casefold()))
    return files


def prepare_uploads(
    roots: list[Path],
    *,
    manifest_path: Path,
    extensions: set[str] | None = None,
    excluded_dir_names: set[str] | None = None,
    output_dir: Path | None = None,
    apply: bool = False,
    mark_existing: bool = False,
    rename_known: bool = False,
    limit: int | None = None,
) -> PrepareSummary:
    wanted_extensions = extensions or set(DEFAULT_MEDIA_EXTENSIONS)
    excluded = excluded_dir_names or set(DEFAULT_EXCLUDED_DIR_NAMES)
    manifest = load_manifest(manifest_path)
    scanned_files = scan_media_files(roots, extensions=wanted_extensions, excluded_dir_names=excluded)
    if limit is not None:
        scanned_files = scanned_files[: max(0, limit)]

    files: list[PreparedFile] = []
    known = marked_existing = planned = renamed = copied = unchanged = 0

    for root, source_path in scanned_files:
        known_file = is_known_file(root, source_path, manifest)
        if known_file:
            known += 1
            if not rename_known:
                continue

        if mark_existing:
            marked_existing += 1
            files.append(PreparedFile(source_path, source_path, "marked_existing"))
            if apply:
                record_file(manifest, root, source_path, source_path, status="marked_existing")
            continue

        cleaned_name = prepared_media_filename(source_path)
        if output_dir:
            target_root = output_dir / root.name if len(roots) > 1 else output_dir
            target_path = target_root / source_path.relative_to(root).parent / cleaned_name
        else:
            target_path = source_path.with_name(cleaned_name)
        if target_path != source_path:
            target_path = unique_target_path(target_path)
        planned += 1

        if target_path == source_path:
            unchanged += 1
            status = "unchanged"
            reason = "filename already clean"
        elif output_dir:
            copied += 1
            status = "copied"
            reason = "copied to output directory"
        else:
            renamed += 1
            status = "renamed"
            reason = "renamed in place"

        files.append(PreparedFile(source_path, target_path, status, reason))
        if apply:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path != source_path:
                if output_dir:
                    shutil.copy2(source_path, target_path)
                else:
                    source_path.rename(target_path)
            if output_dir:
                record_file(manifest, root, source_path, source_path, status=status, output_path=target_path)
            else:
                record_file(manifest, root, source_path, target_path, status=status)

    if apply:
        save_manifest(manifest_path, manifest)

    return PrepareSummary(
        scanned=len(scanned_files),
        known=known,
        marked_existing=marked_existing,
        planned=planned,
        renamed=renamed,
        copied=copied,
        unchanged=unchanged,
        manifest_path=manifest_path,
        dry_run=not apply,
        files=files,
    )


def default_input_dirs_from_env() -> list[str]:
    raw = os.getenv("UPLOAD_INPUT_DIRS", "").strip() or os.getenv("INPUT_DIR", "").strip()
    return parse_path_list(raw) if raw else []
