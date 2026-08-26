"""Tag writing is the only code that mutates user files — verify it is correct,
crash-safe (atomic), backup-capable, and round-trips through a real read."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tag_creator.media import read_media_file, scan_media_files, write_tags
from tag_creator.normalization import normalize_mp3_directories


def test_scan_media_files_ignores_normalized_folder(tmp_path):
    real_file = tmp_path / "Germany Charts 2026" / "Track.mp4"
    normalized_file = tmp_path / "normalized" / "Track.mp4"
    normalization_file = tmp_path / "normalization" / "Track.mp4"
    real_file.parent.mkdir(parents=True)
    normalized_file.parent.mkdir(parents=True)
    normalization_file.parent.mkdir(parents=True)
    real_file.write_bytes(b"mp4")
    normalized_file.write_bytes(b"mp4")
    normalization_file.write_bytes(b"mp4")

    assert scan_media_files(tmp_path, [".mp4"]) == [real_file]


def test_normalize_mp3_directories_writes_copies_next_to_source(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_NORMALIZATION_ENABLED", "true")
    source = tmp_path / "Local Hero MP3" / "Track.mp3"
    skipped_source = tmp_path / "Local Hero MP3" / "normalization" / "Existing.mp3"
    source.parent.mkdir(parents=True)
    skipped_source.parent.mkdir(parents=True)
    source.write_bytes(b"mp3")
    skipped_source.write_bytes(b"mp3")

    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "-f" in command and "null" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr='{"input_i":"-22.0","input_tp":"-2.0","input_lra":"7.0","input_thresh":"-32.0","target_offset":"4.0"}',
            )
        Path(command[-1]).write_bytes(b"normalized")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tag_creator.normalization.subprocess.run", fake_run)

    created, skipped = normalize_mp3_directories(tmp_path)

    assert created == 1
    assert skipped == 0
    assert (source.parent / "normalization" / "Track.mp3").read_bytes() == b"normalized"
    assert len(commands) == 2


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
