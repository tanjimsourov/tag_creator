from __future__ import annotations

from tag_creator.matching import normalize_text, plausible_track_match, similarity


def test_normalize_strips_video_noise():
    assert normalize_text("Song Title (Official Video)") == "song title"
    assert normalize_text("Track [Lyrics]") == "track"


def test_similarity_substring_and_identity():
    assert similarity("hello", "hello") == 1.0
    assert similarity("hello", "hello world") >= 0.5
    assert similarity("", "x") == 0.0


def test_plausible_match_accepts_close_and_rejects_far():
    ok, title_score, artist_score = plausible_track_match("Hello", "Adele", "Hello", "Adele")
    assert ok and title_score >= 0.55 and artist_score >= 0.45

    rejected, _, _ = plausible_track_match("Hello", "Adele", "Totally Different", "Nobody At All")
    assert not rejected


def test_plausible_match_without_input_artist_uses_neutral_score():
    ok, _, artist_score = plausible_track_match("Hello", "", "Hello", "Whoever")
    assert ok and artist_score == 0.65
