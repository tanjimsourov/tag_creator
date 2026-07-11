from __future__ import annotations

import re
from difflib import SequenceMatcher


def normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\[[^\]]+\]|\([^\)]+\)", " ", value)
    value = re.sub(r"\b(official|video|audio|lyrics?|visualizer|remaster(ed)?|radio edit)\b", " ", value)
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm in right_norm or right_norm in left_norm:
        shorter = min(len(left_norm), len(right_norm))
        longer = max(len(left_norm), len(right_norm))
        return max(0.70, shorter / longer)
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def plausible_track_match(
    input_title: str,
    input_artist: str,
    candidate_title: str,
    candidate_artist: str,
    min_title: float = 0.55,
    min_artist: float = 0.45,
) -> tuple[bool, float, float]:
    title_score = similarity(input_title, candidate_title)
    artist_score = similarity(input_artist, candidate_artist) if input_artist else 0.65
    return title_score >= min_title and artist_score >= min_artist, title_score, artist_score

