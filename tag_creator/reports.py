from __future__ import annotations

import csv
import dataclasses
import json
import threading
from pathlib import Path

from .models import EnrichmentResult, MediaFile, RunSummary


def write_run_summary(path: Path, summary: RunSummary) -> None:
    """Write a machine-readable per-run rollup (metrics, guardrails, timings)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(dataclasses.asdict(summary), handle, ensure_ascii=False, indent=2)

# Single source of truth for the enrichment report columns, shared by the
# streaming writer and the legacy full-write helper.
REPORT_FIELDNAMES = [
    "filename",
    "file_path",
    "status",
    "written_fields",
    "missing_required",
    "providers_used",
    "error",
    "title",
    "artist",
    "album",
    "album_artist",
    "year",
    "date",
    "genre",
    "subgenre",
    "mood",
    "moods",
    "energy",
    "valence",
    "danceability",
    "key",
    "language",
    "instruments",
    "vocals",
    "themes",
    "occasion",
    "weather",
    "season",
    "age_group",
    "track_number",
    "disc_number",
    "composer",
    "publisher",
    "copyright",
    "bpm",
    "isrc",
    "comment",
    "lyrics",
    "label",
    "catalog_number",
    "has_cover_art",
    "cover_art_url",
    "analysis_summary",
    "analysis_json",
    "field_confidence_json",
    "field_sources_json",
    "hybrid_stage_summary",
    "notes",
]


FULL_EXPORT_FIELDS = [
    "filename",
    "file_path",
    "size_bytes",
    "duration_seconds",
    "bitrate",
    "title",
    "artist",
    "album",
    "album_artist",
    "year",
    "date",
    "genre",
    "subgenre",
    "mood",
    "moods",
    "energy",
    "valence",
    "danceability",
    "key",
    "language",
    "instruments",
    "vocals",
    "themes",
    "occasion",
    "weather",
    "season",
    "age_group",
    "track_number",
    "disc_number",
    "composer",
    "publisher",
    "copyright",
    "bpm",
    "isrc",
    "comment",
    "lyrics",
    "label",
    "catalog_number",
    "has_cover_art",
    "cover_art_url",
    "analysis_summary",
    "analysis_json",
]


def result_to_row(result: EnrichmentResult) -> dict[str, str]:
    fields = result.merged.fields
    return {
        "filename": result.media.path.name,
        "file_path": str(result.media.path),
        "status": result.status,
        "written_fields": "; ".join(result.written_fields),
        "missing_required": "; ".join(result.merged.missing_required),
        "providers_used": "; ".join(result.merged.providers_used),
        "error": result.error,
        "title": fields.get("title", ""),
        "artist": fields.get("artist", ""),
        "album": fields.get("album", ""),
        "album_artist": fields.get("album_artist", ""),
        "year": fields.get("year", ""),
        "date": fields.get("date", ""),
        "genre": fields.get("genre", ""),
        "subgenre": fields.get("subgenre", ""),
        "mood": fields.get("mood", ""),
        "moods": fields.get("moods", ""),
        "energy": fields.get("energy", ""),
        "valence": fields.get("valence", ""),
        "danceability": fields.get("danceability", ""),
        "key": fields.get("key", ""),
        "language": fields.get("language", ""),
        "instruments": fields.get("instruments", ""),
        "vocals": fields.get("vocals", ""),
        "themes": fields.get("themes", ""),
        "occasion": fields.get("occasion", ""),
        "weather": fields.get("weather", ""),
        "season": fields.get("season", ""),
        "age_group": fields.get("age_group", ""),
        "track_number": fields.get("track_number", ""),
        "disc_number": fields.get("disc_number", ""),
        "composer": fields.get("composer", ""),
        "publisher": fields.get("publisher", ""),
        "copyright": fields.get("copyright", ""),
        "bpm": fields.get("bpm", ""),
        "isrc": fields.get("isrc", ""),
        "comment": fields.get("comment", ""),
        "lyrics": fields.get("lyrics", ""),
        "label": fields.get("label", ""),
        "catalog_number": fields.get("catalog_number", ""),
        "has_cover_art": "yes" if result.media.has_cover_art or fields.get("cover_art_url") else "no",
        "cover_art_url": fields.get("cover_art_url", ""),
        "analysis_summary": fields.get("analysis_summary", ""),
        "analysis_json": fields.get("analysis_json", ""),
        "field_confidence_json": json.dumps(result.merged.field_confidence, ensure_ascii=False),
        "field_sources_json": json.dumps(result.merged.field_sources, ensure_ascii=False),
        "hybrid_stage_summary": stage_summary(result),
        "notes": "; ".join(result.merged.notes),
    }


def stage_summary(result: EnrichmentResult) -> str:
    stages: dict[str, set[str]] = {}
    for provider in result.provider_results:
        stage = "unknown"
        if provider.notes and ":" in provider.notes:
            stage = provider.notes.split(":", 1)[0]
        stages.setdefault(stage, set()).add(provider.provider)
    return "; ".join(f"{stage}={','.join(sorted(providers))}" for stage, providers in sorted(stages.items()))


def _jsonl_record(result: EnrichmentResult) -> dict:
    return {
        "media": {
            "path": str(result.media.path),
            "size_bytes": result.media.size_bytes,
            "mtime": result.media.mtime,
            "tags": result.media.tags,
            "has_cover_art": result.media.has_cover_art,
        },
        "status": result.status,
        "written_fields": result.written_fields,
        "merged": {
            "fields": result.merged.fields,
            "field_confidence": result.merged.field_confidence,
            "field_sources": result.merged.field_sources,
            "missing_required": result.merged.missing_required,
            "providers_used": result.merged.providers_used,
            "notes": result.merged.notes,
        },
        "providers": [
            {
                "provider": provider.provider,
                "confidence": provider.confidence,
                "fields": provider.fields,
                "source_url": provider.source_url,
                "notes": provider.notes,
                "raw": provider.raw,
            }
            for provider in result.provider_results
        ],
        "error": result.error,
    }


class StreamingReportWriter:
    """Incrementally write the enrichment CSV + JSONL as each file completes.

    Memory stays constant regardless of library size: a row is written and
    released immediately instead of buffering every result. Thread-safe so the
    parallel pipeline can call ``write()`` from many worker threads.

    ``append=True`` continues an existing report (resume) instead of truncating
    it, and skips any file whose path is already present in the report.
    """

    FLUSH_EVERY = 200

    def __init__(self, csv_path: Path, append: bool = False) -> None:
        self.csv_path = Path(csv_path)
        self.jsonl_path = self.csv_path.with_suffix(".jsonl")
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending = 0
        self._seen: set[str] = set()

        resume = append and self.csv_path.exists() and self.csv_path.stat().st_size > 0
        if resume:
            self._seen = self._existing_paths(self.csv_path)
        csv_mode = "a" if resume else "w"
        jsonl_mode = "a" if resume else "w"
        # BOM only on a fresh file; appending utf-8-sig would inject a stray BOM.
        csv_encoding = "utf-8" if resume else "utf-8-sig"
        self._csv_file = self.csv_path.open(csv_mode, newline="", encoding=csv_encoding)
        self._writer = csv.DictWriter(self._csv_file, fieldnames=REPORT_FIELDNAMES, extrasaction="ignore")
        if not resume:
            self._writer.writeheader()
        self._jsonl_file = self.jsonl_path.open(jsonl_mode, encoding="utf-8")

    @staticmethod
    def _existing_paths(csv_path: Path) -> set[str]:
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as handle:
                return {row.get("file_path", "") for row in csv.DictReader(handle)}
        except OSError:
            return set()

    def write(self, result: EnrichmentResult) -> bool:
        file_path = str(result.media.path)
        with self._lock:
            if file_path in self._seen:
                return False
            self._seen.add(file_path)
            self._writer.writerow(result_to_row(result))
            self._jsonl_file.write(json.dumps(_jsonl_record(result), ensure_ascii=False) + "\n")
            self._pending += 1
            if self._pending >= self.FLUSH_EVERY:
                self._csv_file.flush()
                self._jsonl_file.flush()
                self._pending = 0
        return True

    def close(self) -> None:
        with self._lock:
            try:
                self._csv_file.flush()
                self._jsonl_file.flush()
            finally:
                self._csv_file.close()
                self._jsonl_file.close()

    def __enter__(self) -> "StreamingReportWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_enrichment_reports(csv_path: Path, results: list[EnrichmentResult]) -> None:
    """Legacy full-write helper — writes all results at once (non-streaming)."""
    with StreamingReportWriter(csv_path, append=False) as writer:
        for result in results:
            writer.write(result)


def export_existing_tags(output_path: Path, media_files: list[MediaFile], preset: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if preset == "basic":
        fieldnames = ["filename", "title", "artist", "album", "year"]
    else:
        fieldnames = FULL_EXPORT_FIELDS
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for media in media_files:
            row = {
                "filename": media.path.name,
                "file_path": str(media.path),
                "size_bytes": str(media.size_bytes),
                "duration_seconds": str(media.duration_seconds or ""),
                "bitrate": str(media.bitrate or ""),
                "has_cover_art": "yes" if media.has_cover_art else "no",
                **media.tags,
            }
            writer.writerow(row)
