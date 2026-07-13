"""Local open-source AI mapping: prove every real model prediction is captured.

These tests use the ACTUAL label formats the Essentia model-zoo models emit
(Discogs genre400 "Parent---Child", mtg_jamendo mood/theme + instrument heads,
and the MSD 50-tag autotagger) and assert the values land in the right fields.
They are pure-function tests — no Essentia install or audio needed — so they run
anywhere and lock in the "100% of predictions mapped" behaviour.
"""
from __future__ import annotations

import json

from tag_creator.clients.local_ai_audio import _features_to_fields, _field_map


def _tags(pairs, head=""):
    return [{"label": label, "score": score, "head": head} for label, score in pairs]


def test_discogs_genre_hierarchy_parent_and_style_are_both_kept():
    tags = _tags(
        [("Electronic---Techno", 0.81), ("Hip Hop---Trap", 0.44), ("Rock---Indie Rock", 0.30)],
        head="genre",
    )
    fields = _field_map(tags, min_score=0.18)
    assert fields["genre"] == "Electronic"
    # "Trap" was dropped by the old keyword-filter mapping — it must be present now.
    assert "Trap" in fields["subgenre"]
    assert "Techno" in fields["subgenre"]


def test_moodtheme_head_splits_mood_theme_occasion_season():
    tags = _tags(
        [("happy", 0.7), ("epic", 0.6), ("christmas", 0.55), ("summer", 0.5), ("party", 0.4)],
        head="mtg_jamendo_moodtheme-discogs-effnet",
    )
    fields = _field_map(tags, min_score=0.18)
    assert fields["mood"] == "happy"
    assert "epic" in fields["moods"]
    assert "christmas" in fields["themes"] and "summer" in fields["themes"]
    assert "christmas" in fields["occasion"] and "party" in fields["occasion"]
    assert fields["season"] in {"winter", "summer"}


def test_instrument_head_maps_instruments_and_vocals():
    tags = _tags(
        [("guitar", 0.8), ("piano", 0.7), ("voice", 0.65), ("electricguitar", 0.4)],
        head="mtg_jamendo_instrument-discogs-effnet",
    )
    fields = _field_map(tags, min_score=0.18)
    assert "guitar" in fields["instruments"]
    assert "piano" in fields["instruments"]
    assert "electric guitar" in fields["instruments"]  # run-together name made readable
    assert fields["vocals"] == "vocal"


def test_msd_flat_autotagger_classifies_by_label():
    tags = _tags(
        [("rock", 0.9), ("happy", 0.7), ("guitar", 0.6), ("female vocalists", 0.5), ("80s", 0.45), ("sad", 0.4)],
        head="msd_autotag",
    )
    fields = _field_map(tags, min_score=0.18)
    assert fields["genre"] == "rock"
    assert "happy" in fields["moods"] and "sad" in fields["moods"]
    assert "guitar" in fields["instruments"]
    assert fields["vocals"] == "vocal"


def test_min_score_filters_but_keeps_full_provenance_json():
    tags = _tags([("rock", 0.5), ("jazz", 0.05)], head="msd_autotag")
    fields = _field_map(tags, min_score=0.18)
    assert fields["genre"] == "rock"
    payload = json.loads(fields["analysis_json"])
    kept = [t["label"] for t in payload["local_ai_top_tags"]]
    assert "rock" in kept and "jazz" not in kept  # below-threshold tag excluded


def test_no_predictions_yields_no_fields():
    assert _field_map([], min_score=0.18) == {}
    assert _field_map([{"label": "rock", "score": 0.01}], min_score=0.18) == {}


def test_essentia_dsp_features_map_to_bpm_key_danceability():
    out = _features_to_fields({"bpm": 128.4, "key": "C#", "scale": "minor", "danceability": 1.5})
    assert out["bpm"] == "128"
    assert out["key"] == "C# minor"
    assert out["danceability"] == "0.5"  # essentia 0-3 range normalized to 0-1


def test_unknown_flat_label_is_not_dropped():
    # An unrecognised descriptor is retained (as a genre-style value), never lost.
    fields = _field_map(_tags([("Vaporwave", 0.6)], head="msd_autotag"), min_score=0.18)
    assert fields.get("genre") == "Vaporwave"
