from __future__ import annotations

import csv
from pathlib import Path

from split import CsvPair, split_pair


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def test_split_pair_writes_top_level_folder_csvs_with_tagged_rows(tmp_path: Path) -> None:
    source = tmp_path / "Local Hero MP4.csv"
    tagged = tmp_path / "Local Hero MP4_with_tag.csv"
    output_root = tmp_path / "clean"
    tagged_headers = ["title", "artist", "filename", "genre", "tag"]
    write_csv(
        source,
        ["filename", "file_path"],
        [
            {
                "filename": "Track One.mp4",
                "file_path": "/app/input_media/DK Rap/Nested/Track One.mp4",
            },
            {
                "filename": "Track Two.mp4",
                "file_path": "/app/input_media/Pop (All times)/Track Two.mp4",
            },
        ],
    )
    write_csv(
        tagged,
        tagged_headers,
        [
            {"title": "Track One", "artist": "Artist", "filename": "Track One.mp4", "genre": "Pop", "tag": "Upbeat"},
            {"title": "Track Two", "artist": "Artist", "filename": "Track Two.mp4", "genre": "Pop", "tag": "Mainstream"},
        ],
    )

    rows, group_count, unmatched = split_pair(
        CsvPair(source, tagged),
        output_root=output_root,
        media_prefixes=("/app/input_media",),
        group_by="top",
        overwrite=True,
    )

    assert (rows, group_count, unmatched) == (2, 2, 0)
    headers, dk_rows = read_csv(output_root / "Local Hero MP4" / "DK Rap.csv")
    assert headers == tagged_headers
    assert dk_rows == [
        {"title": "Track One", "artist": "Artist", "filename": "Track One.mp4", "genre": "Pop", "tag": "Upbeat"}
    ]
    _headers, pop_rows = read_csv(output_root / "Local Hero MP4" / "Pop (All times).csv")
    assert pop_rows[0]["filename"] == "Track Two.mp4"


def test_split_pair_can_group_by_immediate_parent_folder(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    tagged = tmp_path / "input_with_tag.csv"
    write_csv(
        source,
        ["filename", "file_path"],
        [{"filename": "Track One.mp4", "file_path": "/app/input_media/DK Pop/DK Rap/Track One.mp4"}],
    )
    write_csv(
        tagged,
        ["title", "filename", "tag"],
        [{"title": "Track One", "filename": "Track One.mp4", "tag": "Upbeat"}],
    )

    split_pair(
        CsvPair(source, tagged),
        output_root=tmp_path / "clean",
        media_prefixes=("/app/input_media",),
        group_by="parent",
        overwrite=True,
    )

    assert (tmp_path / "clean" / "input" / "DK Rap.csv").exists()


def test_split_pair_keeps_unmatched_rows_in_unmatched_csv(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    tagged = tmp_path / "input_with_tag.csv"
    write_csv(source, ["filename", "file_path"], [])
    write_csv(tagged, ["title", "filename", "tag"], [{"title": "Missing", "filename": "missing.mp4", "tag": "Upbeat"}])

    rows, group_count, unmatched = split_pair(
        CsvPair(source, tagged),
        output_root=tmp_path / "clean",
        media_prefixes=("/app/input_media",),
        group_by="top",
        overwrite=True,
    )

    assert (rows, group_count, unmatched) == (1, 1, 1)
    _headers, unmatched_rows = read_csv(tmp_path / "clean" / "input" / "_unmatched.csv")
    assert unmatched_rows[0]["filename"] == "missing.mp4"
