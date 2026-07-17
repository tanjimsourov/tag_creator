from __future__ import annotations

import csv
import dataclasses
import json
import re
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

FINAL_CSV_FIELDNAMES = [
    "filename",
    "file_path",
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
    "label",
    "catalog_number",
    "has_cover_art",
    "cover_art_url",
    "quality_score",
    "validation_status",
    "missing_tags",
    "sources",
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


def _derive_final_fields(fields: dict[str, str]) -> dict[str, str]:
    derived = dict(fields)
    title = derived.get("title", "").strip()
    artist = derived.get("artist", "").strip()
    album = derived.get("album", "").strip()
    album_artist = derived.get("album_artist", "").strip()
    genre_text = " ".join(
        derived.get(field, "")
        for field in ("genre", "subgenre", "mood", "moods", "themes", "occasion")
    ).lower()
    identity_text = " ".join(item for item in (title, artist, album, album_artist) if item)

    if not derived.get("album_artist") and derived.get("artist"):
        derived["album_artist"] = derived["artist"]
    if not album and title:
        derived["album"] = title
    if not derived.get("date") and derived.get("year"):
        derived["date"] = derived["year"]
    if not derived.get("year") and derived.get("date"):
        derived["year"] = derived["date"][:4]
    if not derived.get("moods") and derived.get("mood"):
        derived["moods"] = derived["mood"]
    if not derived.get("mood") and derived.get("moods"):
        derived["mood"] = derived["moods"].split(",", 1)[0].strip()
    if not derived.get("subgenre") and derived.get("genre"):
        derived["subgenre"] = derived["genre"]
    if not derived.get("track_number"):
        derived["track_number"] = "1"
    if not derived.get("disc_number"):
        derived["disc_number"] = "1"

    mood = derived.get("mood", "").lower()
    energy = derived.get("energy", "").lower()
    danceability = derived.get("danceability", "").lower()
    bpm = derived.get("bpm", "")

    if not derived.get("energy"):
        if any(word in genre_text for word in ("dance", "edm", "house", "techno", "trance", "reggaeton")):
            derived["energy"] = "high"
        elif any(word in genre_text for word in ("ambient", "classical", "acoustic", "ballad", "calm", "chill")):
            derived["energy"] = "low"
        elif bpm.isdigit() and int(bpm) >= 125:
            derived["energy"] = "high"
        elif bpm.isdigit() and int(bpm) <= 85:
            derived["energy"] = "low"
        else:
            derived["energy"] = "medium"

    if not derived.get("mood"):
        if any(word in genre_text for word in ("chill", "ambient", "calm", "relax")):
            derived["mood"] = "calm"
        elif any(word in genre_text for word in ("dance", "edm", "house", "party", "upbeat")):
            derived["mood"] = "upbeat"
        elif "pop" in genre_text:
            derived["mood"] = "mainstream"
        else:
            derived["mood"] = "balanced"
    if not derived.get("moods"):
        derived["moods"] = derived["mood"]

    energy = derived.get("energy", "").lower()
    mood = derived.get("mood", "").lower()
    if derived.get("valence") and not re.fullmatch(r"(?:0(?:\.\d+)?|1(?:\.0+)?)", derived["valence"].strip()):
        valence_text = derived["valence"].lower()
        if any(word in valence_text for word in ("positive", "happy", "bright", "upbeat")):
            derived["valence"] = "0.78"
        elif any(word in valence_text for word in ("negative", "sad", "dark", "melancholic")):
            derived["valence"] = "0.32"
        elif "neutral" in valence_text or "balanced" in valence_text:
            derived["valence"] = "0.58"
    if not derived.get("valence"):
        if any(word in mood for word in ("happy", "upbeat", "party", "sunny", "festive", "positive")):
            derived["valence"] = "0.78"
        elif any(word in mood for word in ("sad", "melancholic", "dark", "dramatic")):
            derived["valence"] = "0.32"
        elif energy == "high":
            derived["valence"] = "0.68"
        elif energy == "low":
            derived["valence"] = "0.46"
        else:
            derived["valence"] = "0.58"

    if derived.get("danceability") and not re.fullmatch(r"(?:0(?:\.\d+)?|1(?:\.0+)?)", derived["danceability"].strip()):
        dance_text = derived["danceability"].lower()
        if any(word in dance_text for word in ("high", "danceable", "club")):
            derived["danceability"] = "0.82"
        elif any(word in dance_text for word in ("low", "non dance", "ambient")):
            derived["danceability"] = "0.35"
        elif "medium" in dance_text or "moderate" in dance_text:
            derived["danceability"] = "0.55"
    if not derived.get("danceability"):
        if any(word in genre_text for word in ("dance", "house", "edm", "techno", "trance", "reggaeton")):
            derived["danceability"] = "0.82"
        elif energy == "high":
            derived["danceability"] = "0.70"
        elif energy == "low":
            derived["danceability"] = "0.35"
        else:
            derived["danceability"] = "0.55"

    if not derived.get("occasion"):
        if energy == "high":
            derived["occasion"] = "busy store"
        elif energy == "low":
            derived["occasion"] = "calm store"
        else:
            derived["occasion"] = "retail background"
    if not derived.get("themes"):
        derived["themes"] = f"{derived.get('mood', 'balanced')} {derived.get('genre', 'music')}".strip()
    if not derived.get("weather"):
        if any(word in genre_text for word in ("latin", "summer", "sunny", "reggaeton")):
            derived["weather"] = "sunny"
        elif any(word in genre_text for word in ("ambient", "calm", "melancholic", "chill")):
            derived["weather"] = "rainy"
        else:
            derived["weather"] = "all weather"
    if not derived.get("season"):
        if "christmas" in genre_text or "holiday" in genre_text:
            derived["season"] = "christmas"
        elif "summer" in genre_text or "latin" in genre_text:
            derived["season"] = "summer"
        else:
            derived["season"] = "all season"
    if not derived.get("age_group"):
        if any(word in genre_text for word in ("dance", "edm", "hip hop", "pop", "reggaeton")):
            derived["age_group"] = "youth/adult"
        else:
            derived["age_group"] = "general"
    if not derived.get("vocals"):
        derived["vocals"] = "vocal"
    if not derived.get("instruments"):
        if any(word in genre_text for word in ("electronic", "dance", "edm", "house", "techno")):
            derived["instruments"] = "electronic beat, synthesizer"
        elif "acoustic" in genre_text:
            derived["instruments"] = "acoustic guitar, vocal"
        else:
            derived["instruments"] = "mixed instrumentation"
    if not derived.get("language"):
        text_for_language = " ".join(item for item in (title, artist, album, album_artist) if item)
        derived["language"] = "English" if re.fullmatch(r"[\x00-\x7F]+", text_for_language or "") else "unknown"

    if not derived.get("label") and derived.get("publisher"):
        derived["label"] = derived["publisher"]
    if not derived.get("publisher") and derived.get("label"):
        derived["publisher"] = derived["label"]
    if not derived.get("label") and derived.get("album_artist"):
        # Best-effort public-source fallback: many downloaded singles do not expose
        # label/publisher through free APIs. Keep the row importable while making
        # the ownership assumption explicit in source/validation fields.
        derived["label"] = derived["album_artist"]
    if not derived.get("publisher") and derived.get("label"):
        derived["publisher"] = derived["label"]
    if not derived.get("composer") and artist:
        derived["composer"] = artist
    if not derived.get("copyright"):
        rights_owner = derived.get("label") or derived.get("publisher") or derived.get("album_artist") or artist
        if rights_owner and derived.get("year"):
            derived["copyright"] = f"(c) {derived['year']} {rights_owner}"
        elif rights_owner:
            derived["copyright"] = f"(c) {rights_owner}"
    if not derived.get("catalog_number") and derived.get("isrc"):
        derived["catalog_number"] = derived["isrc"]
    if not derived.get("comment"):
        derived["comment"] = "Generated by tag_creator from free catalog APIs, local audio AI, and allowlisted public metadata."
    if not derived.get("analysis_summary"):
        summary_bits = [
            f"title={title}" if title else "",
            f"artist={artist}" if artist else "",
            f"genre={derived.get('genre', '')}" if derived.get("genre") else "",
            f"mood={derived.get('mood', '')}" if derived.get("mood") else "",
        ]
        derived["analysis_summary"] = "Final enrichment: " + ", ".join(bit for bit in summary_bits if bit)
    if not derived.get("isrc") and identity_text:
        derived["isrc"] = "not listed in free sources"
    if not derived.get("catalog_number") and identity_text:
        derived["catalog_number"] = "not listed in free sources"
    if not derived.get("cover_art_url"):
        derived["cover_art_url"] = "embedded or not listed in free sources"
    return derived


def _fill_final_blanks(row: dict[str, str], missing_value: str) -> dict[str, str]:
    filled = dict(row)
    generic = "not listed in free sources" if missing_value == "NEEDS_REVIEW" else missing_value
    field_defaults = {
        "title": "unknown title",
        "artist": "unknown artist",
        "album": "single",
        "album_artist": filled.get("artist") or "unknown artist",
        "year": "unknown year",
        "date": filled.get("year") or "unknown date",
        "genre": "pop",
        "subgenre": filled.get("genre") or "popular music",
        "mood": "balanced",
        "moods": filled.get("mood") or "balanced",
        "energy": "medium",
        "valence": "0.58",
        "danceability": "0.55",
        "key": "unknown key",
        "language": "unknown language",
        "instruments": "mixed instrumentation",
        "vocals": "vocal",
        "themes": "retail background music",
        "occasion": "retail background",
        "weather": "all weather",
        "season": "all season",
        "age_group": "general",
        "track_number": "1",
        "disc_number": "1",
        "composer": filled.get("artist") or "unknown composer",
        "publisher": filled.get("label") or filled.get("album_artist") or filled.get("artist") or "not listed in free sources",
        "copyright": "not listed in free sources",
        "bpm": "unknown bpm",
        "isrc": "not listed in free sources",
        "label": filled.get("publisher") or filled.get("album_artist") or filled.get("artist") or "not listed in free sources",
        "catalog_number": filled.get("isrc") or "not listed in free sources",
        "has_cover_art": "no",
        "cover_art_url": "embedded or not listed in free sources",
        "quality_score": "0.0",
        "validation_status": "completed_with_inference",
        "missing_tags": "none",
        "sources": "{}",
    }
    for key in FINAL_CSV_FIELDNAMES:
        value = filled.get(key, "")
        if value is None or str(value).strip() == "":
            filled[key] = field_defaults.get(key, generic)
    return filled


def _final_missing_required(fields: dict[str, str], has_cover_art: bool) -> list[str]:
    required = [
        "title",
        "artist",
        "album",
        "year",
        "genre",
        "subgenre",
        "mood",
        "bpm",
        "key",
        "language",
        "track_number",
        "disc_number",
    ]
    missing = [field for field in required if not str(fields.get(field, "")).strip()]
    if not has_cover_art and not fields.get("cover_art_url"):
        missing.append("cover_art")
    return missing


def result_to_final_row(
    result: EnrichmentResult,
    *,
    no_blanks: bool = False,
    missing_value: str = "NEEDS_REVIEW",
) -> dict[str, str]:
    """Clean dataset row for production import.

    This intentionally excludes debug-heavy report fields such as provider raw
    JSON. It still keeps minimal validation columns so downstream systems know
    whether a row is fully trusted, usable with review, or incomplete.
    """
    fields = _derive_final_fields(result.merged.fields)
    confidences = list(result.merged.field_confidence.values())
    quality_score = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    missing = _final_missing_required(fields, result.media.has_cover_art)
    if result.error or result.status in {"failed", "write_failed"}:
        validation_status = "failed"
    elif missing:
        validation_status = "completed_with_inference" if no_blanks else "needs_review"
    else:
        validation_status = "validated"
    sources = dict(result.merged.field_sources)
    for field in fields:
        sources.setdefault(field, "final_completion")
    row = {
        "filename": result.media.path.name,
        "file_path": str(result.media.path),
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
        "label": fields.get("label", ""),
        "catalog_number": fields.get("catalog_number", ""),
        "has_cover_art": "yes" if result.media.has_cover_art or fields.get("cover_art_url") else "no",
        "cover_art_url": fields.get("cover_art_url", ""),
        "quality_score": str(quality_score),
        "validation_status": validation_status,
        "missing_tags": "; ".join(missing) if missing and not no_blanks else "none",
        "sources": json.dumps(sources, ensure_ascii=False),
    }
    return _fill_final_blanks(row, missing_value) if no_blanks else row


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

    def __init__(
        self,
        csv_path: Path,
        append: bool = False,
        *,
        final_csv: bool = False,
        write_jsonl: bool = True,
        final_no_blanks: bool = False,
        final_missing_value: str = "NEEDS_REVIEW",
    ) -> None:
        self.csv_path = Path(csv_path)
        self.jsonl_path = self.csv_path.with_suffix(".jsonl")
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending = 0
        self._seen: set[str] = set()
        self.final_csv = final_csv
        self.write_jsonl = write_jsonl
        self.final_no_blanks = final_no_blanks
        self.final_missing_value = final_missing_value
        self.fieldnames = FINAL_CSV_FIELDNAMES if final_csv else REPORT_FIELDNAMES

        resume = append and self.csv_path.exists() and self.csv_path.stat().st_size > 0
        if resume:
            self._seen = self._existing_paths(self.csv_path)
        csv_mode = "a" if resume else "w"
        jsonl_mode = "a" if resume else "w"
        # BOM only on a fresh file; appending utf-8-sig would inject a stray BOM.
        csv_encoding = "utf-8" if resume else "utf-8-sig"
        self._csv_file = self.csv_path.open(csv_mode, newline="", encoding=csv_encoding)
        self._writer = csv.DictWriter(self._csv_file, fieldnames=self.fieldnames, extrasaction="ignore")
        if not resume:
            self._writer.writeheader()
        self._jsonl_file = self.jsonl_path.open(jsonl_mode, encoding="utf-8") if write_jsonl else None

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
            self._writer.writerow(
                result_to_final_row(
                    result,
                    no_blanks=self.final_no_blanks,
                    missing_value=self.final_missing_value,
                )
                if self.final_csv
                else result_to_row(result)
            )
            if self._jsonl_file is not None:
                self._jsonl_file.write(json.dumps(_jsonl_record(result), ensure_ascii=False) + "\n")
            self._pending += 1
            if self._pending >= self.FLUSH_EVERY:
                self._csv_file.flush()
                if self._jsonl_file is not None:
                    self._jsonl_file.flush()
                self._pending = 0
        return True

    def close(self) -> None:
        with self._lock:
            try:
                self._csv_file.flush()
                if self._jsonl_file is not None:
                    self._jsonl_file.flush()
            finally:
                self._csv_file.close()
                if self._jsonl_file is not None:
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
