"""Streaming report writer: constant-memory CSV+JSONL, and resume/append that
dedupes by file path and never re-writes the header."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from tag_creator.models import EnrichmentResult, MediaFile, MergedMetadata
from tag_creator.reports import StreamingReportWriter


def _result(path: str, title: str = "T") -> EnrichmentResult:
    media = MediaFile(path=Path(path), extension=".mp3", size_bytes=1, mtime=1.0, tags={})
    merged = MergedMetadata(
        fields={"title": title, "artist": "A"},
        field_confidence={"title": 0.9},
        providers_used=["free"],
        missing_required=[],
        notes=[],
        field_sources={"title": "free"},
    )
    return EnrichmentResult(media, merged, "dry_run_done", [], [])


def test_writer_emits_csv_and_jsonl(tmp_path):
    csv_path = tmp_path / "r.csv"
    writer = StreamingReportWriter(csv_path, append=False)
    assert writer.write(_result("a.mp3")) is True
    writer.close()

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert len(rows) == 1 and rows[0]["title"] == "T"
    jsonl = [json.loads(line) for line in csv_path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
    assert jsonl[0]["merged"]["fields"]["title"] == "T"


def test_writer_creates_header_immediately_and_flushes_each_row(tmp_path):
    csv_path = tmp_path / "live.csv"
    writer = StreamingReportWriter(csv_path, append=False, write_jsonl=False)
    try:
        assert csv_path.exists()
        assert "file_path" in csv_path.read_text(encoding="utf-8-sig")
        writer.write(_result("visible.mp3"))
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
        assert [row["file_path"] for row in rows] == ["visible.mp3"]
    finally:
        writer.close()


def test_resume_append_dedupes_and_keeps_single_header(tmp_path):
    csv_path = tmp_path / "r.csv"
    writer = StreamingReportWriter(csv_path, append=False)
    writer.write(_result("a.mp3"))
    writer.close()

    resumed = StreamingReportWriter(csv_path, append=True)
    assert resumed.write(_result("a.mp3")) is False  # already in the report -> skipped
    assert resumed.write(_result("b.mp3")) is True
    resumed.close()

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert {row["file_path"] for row in rows} == {"a.mp3", "b.mp3"}
    # The header line must appear exactly once even after an append.
    assert csv_path.read_text(encoding="utf-8-sig").count("file_path") == 1
