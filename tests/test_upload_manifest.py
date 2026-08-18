from __future__ import annotations

from pathlib import Path

import pytest

from tag_creator.models import MediaFile
import tag_creator.upload_manifest as upload_manifest
from tag_creator.upload_manifest import (
    cleaned_media_filename,
    embedded_media_filename,
    load_manifest,
    prepare_uploads,
)


def test_cleaned_media_filename_removes_common_video_suffixes() -> None:
    assert cleaned_media_filename("Billie Eilish - LUNCH (Official Music Video).mp4") == "Billie Eilish - LUNCH.mp4"
    assert cleaned_media_filename("Jordan Adetunji - KEHLANI REMIX (feat. Kehlani) [Official Video].mp4") == (
        "Jordan Adetunji - KEHLANI REMIX (feat. Kehlani).mp4"
    )
    assert cleaned_media_filename("Artist: Song? (Official Audio).MP3") == "Artist Song.mp3"


def test_embedded_media_filename_renames_generic_numeric_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "238500.mp3"
    media.write_bytes(b"audio")

    def fake_read_media_file(path: Path) -> MediaFile:
        return MediaFile(
            path=path,
            extension=".mp3",
            size_bytes=media.stat().st_size,
            mtime=media.stat().st_mtime,
            tags={"artist": "Navara", "title": "2 People - Highpass Deep Mix"},
        )

    monkeypatch.setattr(upload_manifest, "read_media_file", fake_read_media_file)

    assert embedded_media_filename(media) == "Navara - 2 People - Highpass Deep Mix.mp3"


def test_embedded_media_filename_keeps_descriptive_artist_title_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    media = tmp_path / "217HALO - Golden Hour.mp3"
    media.write_bytes(b"audio")

    def fake_read_media_file(path: Path) -> MediaFile:
        return MediaFile(
            path=path,
            extension=".mp3",
            size_bytes=media.stat().st_size,
            mtime=media.stat().st_mtime,
            tags={"artist": "Wrong", "title": "Wrong Title"},
        )

    monkeypatch.setattr(upload_manifest, "read_media_file", fake_read_media_file)

    assert embedded_media_filename(media) == ""


def test_mark_existing_then_prepare_only_new_files(tmp_path: Path) -> None:
    root = tmp_path / "LH MP4"
    current = root / "Billboard Charts 2026" / "Existing - Song.mp4"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"existing")
    manifest = tmp_path / "data" / "upload_manifest.json"

    seeded = prepare_uploads([root], manifest_path=manifest, apply=True, mark_existing=True)

    assert seeded.scanned == 1
    assert seeded.marked_existing == 1
    assert manifest.exists()

    new_file = root / "Billboard Charts 2026" / "Billie Eilish - LUNCH (Official Music Video).mp4"
    new_file.write_bytes(b"new")

    processed = prepare_uploads([root], manifest_path=manifest, apply=True)

    renamed = root / "Billboard Charts 2026" / "Billie Eilish - LUNCH.mp4"
    assert processed.scanned == 2
    assert processed.known == 1
    assert processed.renamed == 1
    assert current.exists()
    assert not new_file.exists()
    assert renamed.exists()
    assert len(load_manifest(manifest)["files"]) == 2


def test_prepare_uploads_can_rename_known_generic_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "mp3"
    media = root / "238500.mp3"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"audio")
    manifest = tmp_path / "data" / "upload_manifest.json"

    def fake_read_media_file(path: Path) -> MediaFile:
        return MediaFile(
            path=path,
            extension=".mp3",
            size_bytes=path.stat().st_size,
            mtime=path.stat().st_mtime,
            tags={"artist": "Navara", "title": "2 People - Highpass Deep Mix"},
        )

    monkeypatch.setattr(upload_manifest, "read_media_file", fake_read_media_file)

    seeded = prepare_uploads([root], manifest_path=manifest, apply=True, mark_existing=True)
    skipped = prepare_uploads([root], manifest_path=manifest, apply=True)
    renamed = prepare_uploads([root], manifest_path=manifest, apply=True, rename_known=True)

    target = root / "Navara - 2 People - Highpass Deep Mix.mp3"
    assert seeded.marked_existing == 1
    assert skipped.known == 1
    assert renamed.renamed == 1
    assert not media.exists()
    assert target.exists()
    entries = load_manifest(manifest)["files"]
    assert len(entries) == 1
    entry = next(iter(entries.values()))
    assert entry["original_relpath"] == "238500.mp3"
    assert entry["current_relpath"] == "Navara - 2 People - Highpass Deep Mix.mp3"


def test_prepare_uploads_dry_run_does_not_change_files_or_manifest(tmp_path: Path) -> None:
    root = tmp_path / "LH MP4"
    media = root / "Tik Tok Charts 2026" / "Artist - Song (Official Video).mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    manifest = tmp_path / "data" / "upload_manifest.json"

    summary = prepare_uploads([root], manifest_path=manifest)

    assert summary.dry_run is True
    assert summary.renamed == 1
    assert media.exists()
    assert not (media.parent / "Artist - Song.mp4").exists()
    assert not manifest.exists()


def test_prepare_uploads_handles_name_collisions(tmp_path: Path) -> None:
    root = tmp_path / "LH MP4"
    original = root / "Belgium Charts 2026" / "Artist - Song (Official Video).mp4"
    collision = root / "Belgium Charts 2026" / "Artist - Song.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    collision.write_bytes(b"collision")

    summary = prepare_uploads([root], manifest_path=tmp_path / "manifest.json", apply=True)

    assert summary.renamed == 1
    assert not original.exists()
    assert collision.exists()
    assert (root / "Belgium Charts 2026" / "Artist - Song (2).mp4").exists()
