from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import change as change_module
from change import (
    MissingLanguageResolver,
    MediaDurationResolver,
    OUTPUT_COLUMNS,
    clean_value,
    factual_issues,
    resolve_artist,
    upgrade_csv,
)


def _write_source(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def test_upgrade_applies_portal_rules_and_preserves_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    common = {
        "album": "Single",
        "year": "2024",
        "language": "English",
        "label": "SMC",
        "vocal": "1",
        "instrumental": "0",
        "duration_seconds": "196",
        "subgenre": "Dance-pop, House",
        "mood": "Upbeat",
        "moods": "upbeat",
        "weather": "All Weather",
        "season": "All Season",
        "age_group": "Youth/Adult",
        "bpm": "124",
    }
    _write_source(
        source,
        [
            {
                **common,
                "title": "Summer's Back",
                "artist": "wrong artist",
                "genre": "dance, electronic",
                "filename": "Alok & Jess Glynne - Summer's Back (Lyrics).mp4",
            },
            {
                **common,
                "title": "Summer's Back",
                "artist": "Alok & Jess Glynne",
                "genre": "Electronic",
                "filename": "Alok & Jess Glynne - Summer's Back.mp4",
            },
            {
                **common,
                "title": "Summer's Back",
                "artist": "Alok & Jess Glynne",
                "genre": "Electronic",
                "filename": "duplicate-name.mp4",
            },
        ],
    )

    written, tagged = upgrade_csv(source, output, MediaDurationResolver([]))

    assert source.exists()
    assert written == tagged == 2
    result = _read_rows(output)
    assert list(result[0]) == list(OUTPUT_COLUMNS)
    assert result[0]["title"] == "Summer's Back (Lyrics)"
    assert result[0]["artist"] == "Alok & Jess Glynne"
    assert result[0]["genre"] == "Dance"
    assert result[1]["genre"] == "Dance"
    assert result[0]["time"] == "00:03:16"
    assert result[0]["tag"].split(",").count("Upbeat") == 1
    assert "Dance" not in result[0]["tag"].split(",")


def test_en_dash_filename_repairs_artist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "dash.csv"
    output = tmp_path / "dash_with_tag.csv"
    _write_source(
        source,
        [
            {
                "title": "A New Day",
                "artist": "unknown",
                "album": "Single",
                "genre": "electronic",
                "subgenre": "Progressive House",
                "mood": "Energetic",
                "moods": "Energetic",
                "weather": "All Weather",
                "season": "All Season",
                "age_group": "General",
                "filename": "Sebastian Ingrosso \u2013 A New Day.mp4",
                "year": "2025",
                "language": "English",
                "label": "SMC",
                "vocal": "1",
                "instrumental": "0",
                "time": "00:03:30",
                "tempo": "128",
            }
        ],
    )

    upgrade_csv(source, output, MediaDurationResolver([]))

    result = _read_rows(output)
    assert result[0]["artist"] == "Sebastian Ingrosso"
    assert result[0]["title"] == "A New Day"


def test_complete_source_values_bypass_local_ai_even_with_final_completion_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    media = tmp_path / "Artist - Song.mp4"
    media.write_bytes(b"media")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    _write_source(
        source,
        [
            {
                "title": "Song",
                "artist": "Artist",
                "album": "Single",
                "genre": "Pop",
                "subgenre": "Dance-pop",
                "mood": "Upbeat",
                "weather": "All Weather",
                "season": "All Season",
                "age_group": "Youth/Adult",
                "filename": media.name,
                "year": "2025",
                "language": "English",
                "label": "SMC",
                "vocals": "vocal",
                "bpm": "127",
                "duration_seconds": "180",
                "sources": json.dumps({"vocals": "final_completion", "bpm": "final_completion"}),
            }
        ],
    )

    resolver = MediaDurationResolver([tmp_path], verify_vocals=False)
    monkeypatch.setattr(resolver, "_audio_facts", lambda _path: pytest.fail("local AI must not run"))
    upgrade_csv(source, output, resolver, strict_facts=True)

    result = _read_rows(output)[0]
    assert result["tempo"] == "127"
    assert result["vocal"] == "1"
    assert result["instrumental"] == "0"
    assert result["isDL"] == "1"


def test_existing_vocal_fallback_is_reverified_from_audio(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    media = tmp_path / "Artist - Instrumental.mp4"
    media.write_bytes(b"media")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    _write_source(
        source,
        [
            {
                "title": "Instrumental",
                "artist": "Artist",
                "album": "Single",
                "genre": "Ambient",
                "subgenre": "Ambient",
                "mood": "Calm",
                "weather": "All Weather",
                "season": "All Season",
                "age_group": "General",
                "filename": media.name,
                "year": "2025",
                "language": "English",
                "label": "SMC",
                "vocals": "vocal",
                "bpm": "90",
                "duration_seconds": "180",
            }
        ],
    )

    resolver = MediaDurationResolver([tmp_path], verify_vocals=True)
    monkeypatch.setattr(
        resolver,
        "_audio_facts",
        lambda _path: {"bpm": "90", "vocal": "0", "instrumental": "1"},
    )
    upgrade_csv(source, output, resolver, strict_facts=True)

    result = _read_rows(output)[0]
    assert result["vocal"] == "0"
    assert result["instrumental"] == "1"


def test_missing_vocal_and_bpm_use_local_ai(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    media = tmp_path / "Artist - Song.mp4"
    media.write_bytes(b"media")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    _write_source(
        source,
        [
            {
                "title": "Song",
                "artist": "Artist",
                "album": "Single",
                "genre": "Pop",
                "subgenre": "Dance-pop",
                "mood": "Upbeat",
                "weather": "All Weather",
                "season": "All Season",
                "age_group": "Youth/Adult",
                "filename": media.name,
                "year": "2025",
                "language": "English",
                "label": "SMC",
                "vocals": "",
                "bpm": "unknown bpm",
                "duration_seconds": "180",
            }
        ],
    )

    resolver = MediaDurationResolver([tmp_path])
    monkeypatch.setattr(
        resolver,
        "_audio_facts",
        lambda _path: {"bpm": "127", "vocal": "0", "instrumental": "1"},
    )
    upgrade_csv(source, output, resolver, strict_facts=True)

    result = _read_rows(output)[0]
    assert result["tempo"] == "127"
    assert result["vocal"] == "0"
    assert result["instrumental"] == "1"


def test_media_resolver_accepts_csv_context_folder_under_mounted_root(tmp_path: Path) -> None:
    media_root = tmp_path / "input_media"
    media = media_root / "Belgium Charts 2026" / "Artist - Song.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"media")
    row = {
        "filename": "Artist - Song.mp4",
        "file_path": "/app/input_media/LH MP4/Belgium Charts 2026/Artist - Song.mp4",
    }

    resolved = MediaDurationResolver([media_root]).resolve_media_path(
        row,
        {"filename": "filename", "file_path": "file_path"},
        "LH MP4",
    )

    assert resolved == media


def test_strict_cleaning_removes_unmeasured_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    _write_source(
        source,
        [
            {
                "title": "Song",
                "artist": "Artist",
                "genre": "Pop",
                "filename": "missing.mp3",
                "subgenre": "Dance-pop",
            }
        ],
    )

    rows, tagged_rows = upgrade_csv(source, output, MediaDurationResolver([tmp_path]), strict_facts=True)

    assert (rows, tagged_rows) == (0, 0)
    assert _read_rows(output) == []
    assert "missing_media_rows_skipped=1" in capsys.readouterr().out


def test_duplicate_filename_cleaning_keeps_best_complete_row(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    common = {
        "title": "Song",
        "artist": "Artist",
        "album": "Single",
        "genre": "Pop",
        "subgenre": "Dance-pop",
        "year": "2025",
        "label": "SMC",
        "vocal": "1",
        "instrumental": "0",
        "duration_seconds": "180",
        "bpm": "120",
        "isDL": "1",
    }
    _write_source(
        source,
        [
            {**common, "language": "", "filename": "Artist - Song.mp3"},
            {**common, "language": "English", "filename": "Artist – Song.mp3"},
        ],
    )

    written, tagged = upgrade_csv(source, output, MediaDurationResolver([]), strict_facts=True)

    assert (written, tagged) == (1, 1)
    result = _read_rows(output)
    assert len(result) == 1
    assert result[0]["language"] == "English"


def test_duplicate_filename_cleaning_rejects_shifted_metadata_row(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    filename = "The Rolling Stones - Angry (1080).mp4"
    _write_source(
        source,
        [
            {
                "title": "Angry",
                "artist": "The Rolling Stones",
                "album": "Hackney Diamonds",
                "genre": "Rock",
                "subgenre": "Classic Rock",
                "year": "2023",
                "language": "English",
                "label": "SMC",
                "vocal": "1",
                "instrumental": "0",
                "duration_seconds": "225",
                "bpm": "112",
                "isDL": "1",
                "filename": filename,
            },
            {
                "title": "2023-10-20",
                "artist": "Rock",
                "album": "Rock",
                "genre": "0.58",
                "subgenre": "0.405",
                "year": "energetic",
                "language": "all weather",
                "label": "SMC",
                "vocal": "1",
                "instrumental": "0",
                "duration_seconds": "225",
                "bpm": "https://example.test/cover.jpg",
                "isDL": "1",
                "filename": filename,
            },
        ],
    )

    written, _tagged = upgrade_csv(source, output, MediaDurationResolver([]), strict_facts=True)

    result = _read_rows(output)
    assert written == 1
    assert result[0]["title"] == "Angry"
    assert result[0]["artist"] == "The Rolling Stones"
    assert result[0]["genre"] == "Rock"
    assert result[0]["tempo"] == "112"


def test_shifted_numeric_genre_falls_back_to_media_path_genre(monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    row = {
        "title": "2026-06-01",
        "artist": "Dance",
        "album": "Dance",
        "genre": "0.78",
        "subgenre": "0.372",
        "mood": "E major",
        "year": "2026",
        "language": "English",
        "label": "SMC",
        "vocal": "1",
        "instrumental": "0",
        "duration_seconds": "156",
        "bpm": "129",
        "isDL": "1",
        "filename": "David Guetta ft Alok & Stick Figure - Run Run River (Angels Above Me) (Lyrics).mp4",
        "file_path": "/app/input_media/Dance/Aug 2026/David Guetta ft Alok & Stick Figure - Run Run River (Angels Above Me) (Lyrics).mp4",
    }
    headers = {key: key for key in row}

    result = change_module.build_output_row(row, headers, MediaDurationResolver([]))

    assert result["title"] == "Run Run River (Angels Above Me) (Lyrics)"
    assert result["artist"] == "David Guetta ft Alok & Stick Figure"
    assert result["genre"] == "Dance"
    assert "0.372" not in result["tag"]
    assert "E Major" not in result["tag"]


def test_strict_cleaning_removes_rows_with_missing_required_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    common = {
        "artist": "Artist",
        "album": "Single",
        "genre": "Pop",
        "subgenre": "Dance-pop",
        "year": "2025",
        "label": "SMC",
        "vocal": "1",
        "instrumental": "0",
        "duration_seconds": "180",
        "bpm": "120",
        "isDL": "1",
    }
    _write_source(
        source,
        [
            {**common, "title": "Incomplete", "language": "", "filename": "Artist - Incomplete.mp3"},
            {**common, "title": "Complete", "language": "English", "filename": "Artist - Complete.mp3"},
        ],
    )

    written, tagged = upgrade_csv(source, output, MediaDurationResolver([]), strict_facts=True)

    assert (written, tagged) == (1, 1)
    assert _read_rows(output)[0]["title"] == "Complete"


def test_streamed_output_keeps_completed_rows_after_later_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENRE_API_ENABLED", "false")
    source = tmp_path / "input.csv"
    output = tmp_path / "input_with_tag.csv"
    common = {
        "artist": "Artist",
        "album": "Single",
        "genre": "Pop",
        "subgenre": "Dance-pop",
        "year": "2025",
        "language": "English",
        "label": "SMC",
        "vocal": "1",
        "instrumental": "0",
        "duration_seconds": "180",
        "bpm": "120",
    }
    _write_source(
        source,
        [
            {**common, "title": "First Song", "filename": "Artist - First Song.mp3"},
            {**common, "title": "Second Song", "filename": "Artist - Second Song.mp3"},
        ],
    )

    original_build_output_row = change_module.build_output_row
    calls = 0

    def fail_on_second_row(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("later row failed")
        return original_build_output_row(*args, **kwargs)

    monkeypatch.setattr(change_module, "build_output_row", fail_on_second_row)
    with pytest.raises(RuntimeError, match="later row failed"):
        upgrade_csv(source, output, MediaDurationResolver([]))

    retained_rows = _read_rows(output)
    assert len(retained_rows) == 1
    assert retained_rows[0]["title"] == "First Song"


def test_common_utf8_mojibake_is_repaired() -> None:
    assert clean_value("K\u00c3\u00a6rlighed") == "K\u00e6rlighed"
    assert clean_value("Sydp\u00c3\u00a5") == "Sydp\u00e5"


def test_ambiguous_filename_does_not_invent_smc_artist() -> None:
    row = {"artist": "unknown", "title": "unknown", "filename": "abc123.mp3"}
    headers = {key: key for key in row}

    assert resolve_artist(row, headers) == ""


def test_missing_artist_uses_embedded_media_metadata(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "abc123.mp3"
    media.write_bytes(b"media")
    row = {"artist": "unknown", "title": "Song", "filename": media.name}
    headers = {key: key for key in row}
    resolver = MediaDurationResolver([tmp_path])
    monkeypatch.setattr(
        resolver,
        "_embedded_identity_for_file",
        lambda _path: {"artist": "Verified Artist", "title": "Song"},
    )

    assert resolve_artist(row, headers, resolver) == "Verified Artist"


def test_multi_section_audio_sampling_covers_start_middle_and_end() -> None:
    segments = MediaDurationResolver._analysis_segments(240)

    assert segments == [(29, 15), (113, 15), (197, 15)]


def test_audio_preview_concatenates_all_three_sections(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "Artist - Song.mp4"
    media.write_bytes(b"media")
    resolver = MediaDurationResolver([tmp_path])
    monkeypatch.setattr(resolver, "_duration_for_file", lambda _path: "00:04:00")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs) -> SimpleNamespace:
        commands.append(command)
        Path(command[-1]).write_bytes(b"wav")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("change.subprocess.run", fake_run)
    preview, temporary = resolver._analysis_preview(media)
    try:
        assert temporary == preview
        assert commands[0].count("-i") == 3
        assert "concat=n=3:v=0:a=1" in commands[0][commands[0].index("-filter_complex") + 1]
    finally:
        preview.unlink(missing_ok=True)


def test_known_vocal_classifier_result_maps_to_complementary_flags() -> None:
    result = MediaDurationResolver._classify_vocal_tags(
        [{"label": "voice", "score": 0.91}, {"label": "instrumental", "score": 0.09}],
        voice_labels={"voice"},
        min_confidence=0.60,
        min_margin=0.10,
    )

    assert result["vocal"] == "1"
    assert result["instrumental"] == "0"


def test_known_instrumental_classifier_result_maps_to_complementary_flags() -> None:
    result = MediaDurationResolver._classify_vocal_tags(
        [{"label": "instrumental", "score": 0.88}, {"label": "voice", "score": 0.12}],
        voice_labels={"voice"},
        min_confidence=0.60,
        min_margin=0.10,
    )

    assert result["vocal"] == "0"
    assert result["instrumental"] == "1"


def test_vocal_classifier_rejects_low_confidence_or_unknown_labels() -> None:
    with pytest.raises(ValueError, match="low voice/instrumental confidence"):
        MediaDurationResolver._classify_vocal_tags(
            [{"label": "voice", "score": 0.54}, {"label": "instrumental", "score": 0.46}],
            voice_labels={"voice"},
            min_confidence=0.60,
            min_margin=0.10,
        )

    with pytest.raises(ValueError, match="unexpected voice/instrumental label"):
        MediaDurationResolver._classify_vocal_tags(
            [{"label": "speech", "score": 0.90}, {"label": "instrumental", "score": 0.10}],
            voice_labels={"voice"},
            min_confidence=0.60,
            min_margin=0.10,
        )


def test_final_validation_reports_fake_artist_and_encoding_damage() -> None:
    row = {
        "title": "Broken \ufffd Title",
        "album": "Single",
        "artist": "SMC",
        "time": "00:03:00",
        "genre": "Pop",
        "tempo": "120",
        "filename": "abc123.mp3",
        "year": "2025",
        "language": "English",
        "isDL": "1",
        "label": "SMC",
        "vocal": "1",
        "instrumental": "0",
        "tag": "Upbeat",
    }

    issues = factual_issues(row)
    assert "artist not verified" in issues
    assert "title contains unresolved encoding damage" in issues


def test_missing_language_resolver_preserves_existing_value(monkeypatch) -> None:
    resolver = MissingLanguageResolver()
    monkeypatch.setattr(
        resolver.session,
        "get",
        lambda *args, **kwargs: pytest.fail("lookup must not run for an existing language"),
    )

    assert resolver.resolve(
        "English",
        title="Song",
        artist="Artist",
        album="Single",
        csv_context="100 % Denmark",
    ) == "English"


def test_nordic_refinement_corrects_danish_lyrics_misdetected_as_norwegian() -> None:
    lyrics = (
        "Jeg vil ikke gå med dig. Jeg tænker hele tiden på dig. "
        "Selvom jeg tror på nogen, vil jeg gerne køre med dig."
    )

    assert MissingLanguageResolver._refine_nordic_language(
        lyrics,
        "Norwegian",
        "100 % Denmark",
    ) == "Danish"


def test_nordic_refinement_keeps_real_norwegian() -> None:
    lyrics = "Jeg tenker på deg og meg. Noen kjører uten deg, men hva gjør vi nå?"

    assert MissingLanguageResolver._refine_nordic_language(
        lyrics,
        "Norwegian",
        "Norway",
    ) == "Norwegian"
