"""Tag writing is the only code that mutates user files — verify it is correct,
crash-safe (atomic), backup-capable, and round-trips through a real read."""
from __future__ import annotations

from pathlib import Path

from tag_creator.media import read_media_file, write_tags


def test_write_and_read_back_mp3(sample_mp3):
    written = write_tags(
        sample_mp3,
        {"title": "Neon", "artist": "The Testers", "album": "Demo", "genre": "Techno", "bpm": "128", "date": "2021"},
    )
    assert {"title", "artist", "album", "genre", "bpm"}.issubset(set(written))
    media = read_media_file(sample_mp3)
    assert media.tags["title"] == "Neon"
    assert media.tags["artist"] == "The Testers"
    assert media.tags["genre"] == "Techno"
    assert media.tags["bpm"] == "128"
    assert media.tags["year"] == "2021"


def test_write_is_atomic_leaves_no_temp_file(sample_mp3):
    write_tags(sample_mp3, {"title": "X"})
    leftovers = list(sample_mp3.parent.glob(".*tagtmp*"))
    assert leftovers == []
    assert read_media_file(sample_mp3).tags["title"] == "X"  # still a valid MP3


def test_backup_copies_pristine_original(tmp_path, sample_mp3):
    backup = tmp_path / "backup"
    write_tags(sample_mp3, {"title": "New"}, backup_dir=backup, input_root=sample_mp3.parent)
    backed = list(backup.rglob("*.mp3"))
    assert backed, "expected a pristine backup copy before writing"


def test_verify_after_write_runs_and_confirms(sample_mp3):
    written = write_tags(sample_mp3, {"title": "Verified", "artist": "A"}, verify=True)
    assert "title" in written
    assert read_media_file(sample_mp3).tags["title"] == "Verified"


def test_unsupported_extension_is_skipped(tmp_path):
    path = tmp_path / "x.wav"
    path.write_bytes(b"RIFF0000WAVEfmt ")
    assert write_tags(path, {"title": "x"}) == []
