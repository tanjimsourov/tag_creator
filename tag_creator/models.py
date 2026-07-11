from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


COMMON_TAG_FIELDS = [
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
    "cover_art_url",
    "analysis_summary",
    "analysis_json",
]


@dataclass
class MediaFile:
    path: Path
    extension: str
    size_bytes: int
    mtime: float
    duration_seconds: int | None = None
    bitrate: int | None = None
    tags: dict[str, str] = field(default_factory=dict)
    has_cover_art: bool = False


@dataclass
class ProviderResult:
    provider: str
    confidence: float
    fields: dict[str, str]
    source_url: str = ""
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MergedMetadata:
    fields: dict[str, str]
    field_confidence: dict[str, float]
    providers_used: list[str]
    missing_required: list[str]
    notes: list[str]
    field_sources: dict[str, str] = field(default_factory=dict)


@dataclass
class EnrichmentResult:
    media: MediaFile
    merged: MergedMetadata
    status: str
    written_fields: list[str]
    provider_results: list[ProviderResult]
    error: str = ""


@dataclass
class RunSummary:
    """Lightweight per-run rollup so the pipeline never retains every result."""

    report_path: str = ""
    total_files: int = 0
    skipped: int = 0
    written_rows: int = 0
    interrupted: bool = False
    status_counts: dict[str, int] = field(default_factory=dict)
    provider_hits: dict[str, int] = field(default_factory=dict)
    provider_errors: dict[str, int] = field(default_factory=dict)
    provider_latency_ms: dict[str, float] = field(default_factory=dict)
    paid_calls: int = 0
    web_fetches: int = 0
    duration_seconds: float = 0.0
    workers: int = 1

    def count(self, status: str) -> int:
        return self.status_counts.get(status, 0)
