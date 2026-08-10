from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from change import MissingLanguageResolver, MediaDurationResolver, OUTPUT_COLUMNS, clean_value, upgrade_csv


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

    resolver = MediaDurationResolver([tmp_path])
    monkeypatch.setattr(resolver, "_audio_facts", lambda _path: pytest.fail("local AI must not run"))
    upgrade_csv(source, output, resolver, strict_facts=True)

    result = _read_rows(output)[0]
    assert result["tempo"] == "127"
    assert result["vocal"] == "1"
    assert result["instrumental"] == "0"
    assert result["isDL"] == "1"


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


def test_strict_final_rejects_unmeasured_facts_without_replacing_output(tmp_path: Path, monkeypatch) -> None:
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

    with pytest.raises(ValueError, match="final CSV rejected"):
        upgrade_csv(source, output, MediaDurationResolver([tmp_path]), strict_facts=True)
    assert not output.exists()


def test_common_utf8_mojibake_is_repaired() -> None:
    assert clean_value("K\u00c3\u00a6rlighed") == "K\u00e6rlighed"


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
