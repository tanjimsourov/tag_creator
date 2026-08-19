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
    headers, dk_rows = read_csv(output_root / "Local Hero MP4" / "DK Rap.xls")
    assert headers == tagged_headers
    assert dk_rows == [
        {"title": "Track One", "artist": "Artist", "filename": "Track One.mp4", "genre": "Pop", "tag": "Upbeat"}
    ]
    _headers, pop_rows = read_csv(output_root / "Local Hero MP4" / "Pop (All times).xls")
    assert pop_rows[0]["filename"] == "Track Two.mp4"


def test_split_pair_groups_root_level_media_by_source_name(tmp_path: Path) -> None:
    source = tmp_path / "Bowlnfun Sing King Karaoke.csv"
    tagged = tmp_path / "Bowlnfun Sing King Karaoke_with_tag.xls"
    output_root = tmp_path / "clean"
    write_csv(
        source,
        ["filename", "file_path"],
        [
            {
                "filename": "2step - Ed Sheeran feat. Lil Baby (Karaoke).mp4",
                "file_path": "/app/input_media/2step - Ed Sheeran feat. Lil Baby (Karaoke).mp4",
            },
            {
                "filename": "Christmas Song.mp4",
                "file_path": "/app/input_media/Sing King Christian & Christmas/Christmas Song.mp4",
            },
        ],
    )
    write_csv(
        tagged,
        ["title", "filename", "tag"],
        [
            {
                "title": "2step",
                "filename": "2step - Ed Sheeran feat. Lil Baby (Karaoke).mp4",
                "tag": "Karaoke",
            },
            {"title": "Christmas Song", "filename": "Christmas Song.mp4", "tag": "Christmas"},
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
    _headers, root_rows = read_csv(output_root / "Bowlnfun Sing King Karaoke" / "Bowlnfun Sing King Karaoke.xls")
    assert root_rows[0]["filename"] == "2step - Ed Sheeran feat. Lil Baby (Karaoke).mp4"
    _headers, christmas_rows = read_csv(
        output_root / "Bowlnfun Sing King Karaoke" / "Sing King Christian & Christmas.xls"
    )
    assert christmas_rows[0]["filename"] == "Christmas Song.mp4"


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

    assert (tmp_path / "clean" / "input" / "DK Rap.xls").exists()


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
    _headers, unmatched_rows = read_csv(tmp_path / "clean" / "input" / "_unmatched.xls")
    assert unmatched_rows[0]["filename"] == "missing.mp4"


def test_split_pair_with_media_root_skips_rows_without_existing_media(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    (media_root / "Existing").mkdir(parents=True)
    (media_root / "Existing" / "Keep.mp3").write_bytes(b"audio")
    stale_output = tmp_path / "clean" / "LH MP3" / "Empty.csv"
    write_csv(stale_output, ["title", "filename", "tag"], [{"title": "Old", "filename": "Old.mp3", "tag": "Old"}])

    source = tmp_path / "LH MP3.csv"
    tagged = tmp_path / "LH MP3_with_tag.csv"
    write_csv(
        source,
        ["filename", "file_path"],
        [
            {"filename": "Keep.mp3", "file_path": "/app/input_media/Existing/Keep.mp3"},
            {"filename": "Skip.mp3", "file_path": "/app/input_media/Empty/Skip.mp3"},
        ],
    )
    write_csv(
        tagged,
        ["title", "filename", "tag"],
        [
            {"title": "Keep", "filename": "Keep.mp3", "tag": "Upbeat"},
            {"title": "Skip", "filename": "Skip.mp3", "tag": "Balanced"},
        ],
    )

    rows, group_count, unmatched = split_pair(
        CsvPair(source, tagged),
        output_root=tmp_path / "clean",
        media_prefixes=("/app/input_media",),
        group_by="top",
        media_roots=(media_root,),
        overwrite=True,
    )

    assert (rows, group_count, unmatched) == (1, 1, 1)
    _headers, existing_rows = read_csv(tmp_path / "clean" / "LH MP3" / "Existing.xls")
    assert existing_rows == [{"title": "Keep", "filename": "Keep.mp3", "tag": "Upbeat"}]
    assert not (tmp_path / "clean" / "LH MP3" / "_unmatched.xls").exists()
    assert not (tmp_path / "clean" / "LH MP3" / "Empty.csv").exists()


def test_split_pair_accepts_csv_context_folder_under_mounted_root(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    (media_root / "Belgium Charts 2026").mkdir(parents=True)
    (media_root / "Belgium Charts 2026" / "Keep.mp4").write_bytes(b"video")

    source = tmp_path / "LH MP4.csv"
    tagged = tmp_path / "LH MP4_with_tag.csv"
    write_csv(
        source,
        ["filename", "file_path"],
        [
            {
                "filename": "Keep.mp4",
                "file_path": "/app/input_media/LH MP4/Belgium Charts 2026/Keep.mp4",
            }
        ],
    )
    write_csv(
        tagged,
        ["title", "filename", "tag"],
        [{"title": "Keep", "filename": "Keep.mp4", "tag": "Upbeat"}],
    )

    rows, group_count, unmatched = split_pair(
        CsvPair(source, tagged),
        output_root=tmp_path / "clean",
        media_prefixes=("/app/input_media",),
        group_by="top",
        media_roots=(media_root,),
        overwrite=True,
    )

    assert (rows, group_count, unmatched) == (1, 1, 0)
    _headers, rows = read_csv(tmp_path / "clean" / "LH MP4" / "Belgium Charts 2026.xls")
    assert rows == [{"title": "Keep", "filename": "Keep.mp4", "tag": "Upbeat"}]


def test_split_pair_can_write_legacy_csv_when_requested(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    tagged = tmp_path / "input_with_tag.xls"
    write_csv(
        source,
        ["filename", "file_path"],
        [{"filename": "Track One.mp4", "file_path": "/app/input_media/DK Pop/Track One.mp4"}],
    )
    write_csv(tagged, ["title", "filename", "tag"], [{"title": "Track One", "filename": "Track One.mp4", "tag": "Upbeat"}])

    split_pair(
        CsvPair(source, tagged),
        output_root=tmp_path / "clean",
        media_prefixes=("/app/input_media",),
        group_by="top",
        output_extension=".csv",
        overwrite=True,
    )

    assert (tmp_path / "clean" / "input" / "DK Pop.csv").exists()
