from __future__ import annotations

from pathlib import Path

from tag_creator.models import MediaFile, MergedMetadata, ProviderResult
from tag_creator.scoring import fields_to_write, merge_metadata


def _media(tags=None) -> MediaFile:
    return MediaFile(path=Path("/lib/a.mp3"), extension=".mp3", size_bytes=1, mtime=1.0, tags=tags or {})


def test_high_confidence_fields_land_and_missing_reported():
    results = [ProviderResult("itunes", 1.0, {"title": "Hello", "artist": "Adele"})]
    merged = merge_metadata(
        _media(), results, {"itunes": 0.9}, min_field_confidence=0.72,
        required_tags=["title", "artist", "album"],
    )
    assert merged.fields["title"] == "Hello"
    assert merged.fields["artist"] == "Adele"
    assert "album" in merged.missing_required


def test_low_effective_confidence_is_filtered():
    # weight*confidence = 0.5 < 0.72 -> field must not land
    results = [ProviderResult("weak", 1.0, {"title": "Maybe"})]
    merged = merge_metadata(_media(), results, {"weak": 0.5}, 0.72, ["title"])
    assert "title" not in merged.fields
    assert "title" in merged.missing_required


def test_conflicting_providers_emit_conflict_note():
    results = [ProviderResult("itunes", 1.0, {"title": "A"}), ProviderResult("deezer", 1.0, {"title": "B"})]
    merged = merge_metadata(_media(), results, {"itunes": 0.9, "deezer": 0.9}, 0.72, ["title"])
    assert any("conflict on title" in note for note in merged.notes)


def test_year_canonicalization_agreement():
    results = [
        ProviderResult("itunes", 1.0, {"year": "2011"}),
        ProviderResult("deezer", 1.0, {"year": "2011-01-24"}),
    ]
    merged = merge_metadata(_media(), results, {"itunes": 0.9, "deezer": 0.9}, 0.72, ["year"])
    assert merged.fields["year"] == "2011"


def test_fields_to_write_respects_threshold_and_diff():
    media = _media({"title": "Old"})
    merged = MergedMetadata(
        fields={"title": "New", "artist": "X"},
        field_confidence={"title": 0.9, "artist": 0.5},
        providers_used=[], missing_required=[], notes=[],
    )
    assert fields_to_write(media, merged, min_write_confidence=0.82) == {"title": "New"}


def test_fields_to_write_skips_equivalent_value():
    media = _media({"title": "Same Song"})
    merged = MergedMetadata(
        fields={"title": "same song"},
        field_confidence={"title": 0.99},
        providers_used=[], missing_required=[], notes=[],
    )
    assert fields_to_write(media, merged, 0.82) == {}
