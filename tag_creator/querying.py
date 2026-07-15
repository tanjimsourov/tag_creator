from __future__ import annotations

import re
from pathlib import Path

from .models import MediaFile


NOISE_WORDS = (
    "official music video",
    "official video",
    "official audio",
    "music video",
    "lyric video",
    "lyrics",
    "visualizer",
    "audio",
    "video",
    "remastered",
    "remaster",
    "radio edit",
)


def clean_track_text(value: str) -> str:
    cleaned = Path(value).stem if value else ""
    cleaned = re.sub(r"[_]+", " ", cleaned)
    cleaned = re.sub(r"\[[^\]]+\]|\([^\)]+\)", " ", cleaned)
    for word in NOISE_WORDS:
        cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{3,}\s*kbps\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(mp3|mp4|m4a|aac|flac)\b", " ", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned).strip(" -_")


def split_artist_title(value: str) -> tuple[str, str]:
    cleaned = clean_track_text(value)
    for separator in (r"\s+-\s+", r"\s+--\s+", r"\s+\|\s+"):
        parts = re.split(separator, cleaned, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return "", cleaned


def candidate_track_pairs(media: MediaFile, limit: int = 4) -> list[tuple[str, str]]:
    """Build conservative artist/title candidates from tags and filename.

    Providers differ in how they search, so using a few clean candidates improves
    hit rate without broad scraping. Ordering favors embedded tags, then parsed
    filename/title fallbacks.
    """

    title = clean_track_text(media.tags.get("title", ""))
    artist = clean_track_text(media.tags.get("artist", ""))
    album = clean_track_text(media.tags.get("album", ""))
    stem_artist, stem_title = split_artist_title(media.path.stem)
    title_artist, title_title = split_artist_title(title)

    raw_pairs = [
        (artist, title),
        (stem_artist, stem_title),
        (title_artist or artist, title_title),
        (artist, album),
        ("", stem_title or title),
    ]
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_artist, raw_title in raw_pairs:
        item = (clean_track_text(raw_artist), clean_track_text(raw_title))
        if not item[1] or item in seen:
            continue
        seen.add(item)
        pairs.append(item)
        if len(pairs) >= limit:
            break
    return pairs


def search_texts(media: MediaFile, limit: int = 4) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for artist, title in candidate_track_pairs(media, limit=limit):
        query = " ".join(part for part in (artist, title) if part).strip()
        if query and query.lower() not in seen:
            seen.add(query.lower())
            queries.append(query)
    return queries
