from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import requests
from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TBPM,
    TCOM,
    TCON,
    TCOP,
    TDRC,
    TENC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from .models import MediaFile

LOGGER = logging.getLogger(__name__)

# field name -> single-valued ID3 text frame class (date/year handled separately)
_MP3_TEXT_FRAMES = {
    "title": TIT2,
    "artist": TPE1,
    "album": TALB,
    "album_artist": TPE2,
    "genre": TCON,
    "track_number": TRCK,
    "disc_number": TPOS,
    "composer": TCOM,
    "bpm": TBPM,
    "isrc": TSRC,
    "copyright": TCOP,
    "encoded_by": TENC,
}


def scan_media_files(input_dir: Path, extensions: list[str], limit: int | None = None) -> list[Path]:
    wanted = {extension.lower() for extension in extensions}
    files = [
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in wanted
    ]
    files.sort()
    return files[:limit] if limit is not None else files


def _first(values: list[str] | None) -> str:
    if not values:
        return ""
    return str(values[0]).strip()


def _read_mp3(path: Path) -> MediaFile:
    audio = MP3(path)
    try:
        easy = EasyID3(path)
    except ID3NoHeaderError:
        easy = EasyID3()
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = None

    tags = {
        "title": _first(easy.get("title")),
        "artist": _first(easy.get("artist")),
        "album": _first(easy.get("album")),
        "album_artist": _first(easy.get("albumartist")),
        "date": _first(easy.get("date")),
        "year": _first(easy.get("date"))[:4],
        "genre": _first(easy.get("genre")),
        "track_number": _first(easy.get("tracknumber")),
        "disc_number": _first(easy.get("discnumber")),
        "composer": _first(easy.get("composer")),
        "bpm": _first(easy.get("bpm")),
        "isrc": _first(easy.get("isrc")),
        "copyright": _first(easy.get("copyright")),
        "encoded_by": _first(easy.get("encodedby")),
    }
    if id3:
        for frame in id3.values():
            if isinstance(frame, COMM) and frame.text:
                tags["comment"] = " ".join(str(item).strip() for item in frame.text if str(item).strip())
                break
    has_cover = bool(id3 and any(isinstance(frame, APIC) for frame in id3.values()))
    stat = path.stat()
    return MediaFile(
        path=path,
        extension=path.suffix.lower(),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        duration_seconds=int(audio.info.length) if audio.info and audio.info.length else None,
        bitrate=int(audio.info.bitrate) if audio.info and audio.info.bitrate else None,
        tags={key: value for key, value in tags.items() if value},
        has_cover_art=has_cover,
    )


def _mp4_first(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, tuple):
        return "/".join(str(item) for item in value if item)
    return str(value).strip()


def _read_mp4(path: Path) -> MediaFile:
    audio = MP4(path)
    mp4tags = audio.tags or {}
    tags = {
        "title": _mp4_first(mp4tags.get("\xa9nam")),
        "artist": _mp4_first(mp4tags.get("\xa9ART")),
        "album": _mp4_first(mp4tags.get("\xa9alb")),
        "album_artist": _mp4_first(mp4tags.get("aART")),
        "date": _mp4_first(mp4tags.get("\xa9day")),
        "year": _mp4_first(mp4tags.get("\xa9day"))[:4],
        "genre": _mp4_first(mp4tags.get("\xa9gen")),
        "composer": _mp4_first(mp4tags.get("\xa9wrt")),
        "bpm": _mp4_first(mp4tags.get("tmpo")),
    }
    if mp4tags.get("trkn"):
        track = mp4tags["trkn"][0]
        tags["track_number"] = str(track[0]) if track and track[0] else ""
    if mp4tags.get("disk"):
        disc = mp4tags["disk"][0]
        tags["disc_number"] = str(disc[0]) if disc and disc[0] else ""
    has_cover = bool(mp4tags.get("covr"))
    stat = path.stat()
    return MediaFile(
        path=path,
        extension=path.suffix.lower(),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        duration_seconds=int(audio.info.length) if audio.info and audio.info.length else None,
        bitrate=int(audio.info.bitrate) if audio.info and audio.info.bitrate else None,
        tags={key: value for key, value in tags.items() if value},
        has_cover_art=has_cover,
    )


def read_media_file(path: Path) -> MediaFile:
    if path.suffix.lower() == ".mp3":
        return _read_mp3(path)
    if path.suffix.lower() in {".m4a", ".mp4", ".aac"}:
        return _read_mp4(path)

    audio = MutagenFile(path)
    stat = path.stat()
    tags: dict[str, str] = {}
    if audio and audio.tags:
        for key, value in audio.tags.items():
            tags[str(key)] = _mp4_first(value)
    return MediaFile(
        path=path,
        extension=path.suffix.lower(),
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        duration_seconds=int(audio.info.length) if audio and audio.info and audio.info.length else None,
        bitrate=int(audio.info.bitrate) if audio and audio.info and getattr(audio.info, "bitrate", None) else None,
        tags=tags,
        has_cover_art=False,
    )


def _download_cover(url: str) -> tuple[bytes, str] | None:
    response = requests.get(url, timeout=(10, 30))
    if not response.ok:
        return None
    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
    if not content_type.startswith("image/"):
        return None
    return response.content, content_type


def _backup_original(path: Path, backup_dir: Path, input_root: Path | None) -> None:
    """Copy the pristine original into backup_dir once (never overwrite a backup)."""
    try:
        relative = path.relative_to(input_root) if input_root else Path(path.name)
    except ValueError:
        relative = Path(path.name)
    destination = backup_dir / relative
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _verify_written(path: Path, fields: dict[str, str], written: list[str]) -> list[str]:
    from .matching import normalize_text

    reread = read_media_file(path)
    mismatches: list[str] = []
    for field in written:
        if field in {"cover_art", "comment"}:
            continue
        intended = fields.get(field, "")
        if field in {"year", "date"}:
            wanted = intended[:4]
            got = (reread.tags.get("year", "") or reread.tags.get("date", ""))[:4]
            if wanted and wanted != got:
                mismatches.append(field)
            continue
        actual = reread.tags.get(field, "")
        if normalize_text(intended) and normalize_text(intended) != normalize_text(actual):
            mismatches.append(field)
    if mismatches:
        LOGGER.warning("verify: %s did not read back as written: %s", path.name, ", ".join(sorted(set(mismatches))))
    return mismatches


def write_tags(
    path: Path,
    fields: dict[str, str],
    write_cover_art: bool = False,
    backup_dir: Path | None = None,
    input_root: Path | None = None,
    verify: bool = False,
) -> list[str]:
    """Write tags crash-safely: edit a temp copy, then atomically replace the original.

    A kill mid-write can only leave the original intact or the fully-written temp,
    never a half-written original. An optional backup of the pristine original is
    taken before the replace.
    """
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        tag_writer = _write_mp3
    elif suffix in {".m4a", ".mp4", ".aac"}:
        tag_writer = _write_mp4
    else:
        LOGGER.warning("Writing tags for %s is not supported yet", path.suffix)
        return []

    if backup_dir:
        _backup_original(path, backup_dir, input_root)

    temp_path = path.with_name(f".{path.name}.tagtmp{path.suffix}")
    shutil.copy2(path, temp_path)
    try:
        written = tag_writer(temp_path, fields, write_cover_art)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    if verify and written:
        _verify_written(path, fields, written)
    return written


def _write_mp3(path: Path, fields: dict[str, str], write_cover_art: bool) -> list[str]:
    """Single-pass ID3 write: all text/comment/cover frames set on one object."""
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()

    written: list[str] = []

    date_value = fields.get("date") or fields.get("year")
    if date_value:
        id3.setall("TDRC", [TDRC(encoding=3, text=date_value)])
        written.append("date" if fields.get("date") else "year")

    for field, frame_cls in _MP3_TEXT_FRAMES.items():
        value = fields.get(field, "")
        if not value:
            continue
        id3.setall(frame_cls.__name__, [frame_cls(encoding=3, text=value)])
        written.append(field)

    if fields.get("comment"):
        id3.delall("COMM")
        id3.add(COMM(encoding=3, lang="eng", desc="", text=fields["comment"]))
        written.append("comment")

    if write_cover_art and fields.get("cover_art_url"):
        cover = _download_cover(fields["cover_art_url"])
        if cover:
            data, mime = cover
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            written.append("cover_art")

    id3.save(path, v2_version=3)
    return sorted(set(written))


def _write_mp4(path: Path, fields: dict[str, str], write_cover_art: bool) -> list[str]:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    mapping = {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "album_artist": "aART",
        "date": "\xa9day",
        "year": "\xa9day",
        "genre": "\xa9gen",
        "composer": "\xa9wrt",
    }
    written: list[str] = []
    for field, mp4_key in mapping.items():
        value = fields.get(field, "")
        if not value:
            continue
        if field == "year" and fields.get("date"):
            continue
        tags[mp4_key] = [value]
        written.append(field)
    if fields.get("track_number", "").isdigit():
        tags["trkn"] = [(int(fields["track_number"]), 0)]
        written.append("track_number")
    if fields.get("disc_number", "").isdigit():
        tags["disk"] = [(int(fields["disc_number"]), 0)]
        written.append("disc_number")
    if fields.get("bpm", "").isdigit():
        tags["tmpo"] = [int(fields["bpm"])]
        written.append("bpm")
    if write_cover_art and fields.get("cover_art_url"):
        cover = _download_cover(fields["cover_art_url"])
        if cover:
            data, mime = cover
            image_format = MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(data, imageformat=image_format)]
            written.append("cover_art")
    audio.save()
    return sorted(set(written))

