from __future__ import annotations

import re
from collections import defaultdict

from .models import COMMON_TAG_FIELDS, MediaFile, MergedMetadata, ProviderResult


def normalize(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _canonical_vote(field: str, value: str) -> str:
    value = value.strip()
    if field == "date":
        match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})(?:\b|T)", value)
        if match:
            return "-".join(match.groups())
    if field == "year":
        match = re.search(r"\b(19\d{2}|20\d{2})\b", value)
        if match:
            return match.group(1)
    if field in {"track_number", "disc_number"}:
        match = re.search(r"\d+", value)
        if match:
            return str(int(match.group(0)))
    if field == "bpm":
        match = re.search(r"\d+", value)
        if match:
            return match.group(0)
    return value


def _missing_required(media: MediaFile, fields: dict[str, str], required_tags: list[str]) -> list[str]:
    missing: list[str] = []
    for tag in required_tags:
        if tag == "cover_art":
            if not media.has_cover_art and not fields.get("cover_art_url"):
                missing.append(tag)
        elif not fields.get(tag) and not media.tags.get(tag):
            missing.append(tag)
    return missing


def _conflict_notes(votes: dict[str, dict[str, float]]) -> list[str]:
    conflicts: list[str] = []
    for field, field_votes in votes.items():
        normalized_values: dict[str, list[str]] = defaultdict(list)
        for value in field_votes:
            normalized_values[normalize(value)].append(value)
        if len([key for key in normalized_values if key]) <= 1:
            continue
        ranked = sorted(field_votes.items(), key=lambda item: item[1], reverse=True)
        summary = ", ".join(f"{value} ({score:.2f})" for value, score in ranked[:4])
        conflicts.append(f"conflict on {field}: {summary}")
    return conflicts


def merge_metadata(
    media: MediaFile,
    results: list[ProviderResult],
    provider_weights: dict[str, float],
    min_field_confidence: float,
    required_tags: list[str],
) -> MergedMetadata:
    votes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    vote_sources: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    notes: list[str] = []

    for field, value in media.tags.items():
        if field in COMMON_TAG_FIELDS and value:
            canonical = _canonical_vote(field, value)
            votes[field][canonical] += 0.76
            vote_sources[field][canonical].append("existing_file_tag")

    for result in results:
        provider_weight = provider_weights.get(result.provider, 0.5)
        effective_confidence = max(0.0, min(1.0, result.confidence * provider_weight))
        for field in COMMON_TAG_FIELDS:
            value = result.fields.get(field, "").strip()
            if not value:
                continue
            canonical = _canonical_vote(field, value)
            votes[field][canonical] += effective_confidence
            vote_sources[field][canonical].append(result.provider)
        if result.notes:
            notes.append(f"{result.provider}: {result.notes}")

    notes.extend(_conflict_notes(votes))

    merged_fields: dict[str, str] = {}
    field_confidence: dict[str, float] = {}
    field_sources: dict[str, str] = {}

    for field, field_votes in votes.items():
        best_value, best_score = sorted(field_votes.items(), key=lambda item: item[1], reverse=True)[0]
        supporting_score = min(1.0, best_score)
        if supporting_score >= min_field_confidence:
            merged_fields[field] = best_value
            field_confidence[field] = round(supporting_score, 3)
            field_sources[field] = ", ".join(sorted(set(vote_sources[field][best_value])))
        else:
            field_confidence[field] = round(supporting_score, 3)

    # Existing tags outside the known field list are preserved only when no provider
    # participates; common fields already vote above.
    for field, value in media.tags.items():
        if field in COMMON_TAG_FIELDS and value and field not in merged_fields:
            merged_fields[field] = value
            field_confidence[field] = max(field_confidence.get(field, 0), 0.70)
            field_sources[field] = "existing_file_tag"

    missing = _missing_required(media, merged_fields, required_tags)
    providers_used = sorted({result.provider for result in results if result.fields})
    return MergedMetadata(
        fields=merged_fields,
        field_confidence=field_confidence,
        providers_used=providers_used,
        missing_required=missing,
        notes=notes,
        field_sources=field_sources,
    )


def fields_to_write(media: MediaFile, merged: MergedMetadata, min_write_confidence: float) -> dict[str, str]:
    writable: dict[str, str] = {}
    for field, value in merged.fields.items():
        if not value:
            continue
        current = media.tags.get(field, "")
        if current and normalize(current) == normalize(value):
            continue
        if merged.field_confidence.get(field, 0) >= min_write_confidence:
            writable[field] = value
    return writable
