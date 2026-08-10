from __future__ import annotations

import csv
from pathlib import Path

from change import MediaDurationResolver, OUTPUT_COLUMNS, upgrade_csv


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
