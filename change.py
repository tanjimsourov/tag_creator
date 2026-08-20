from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from argparse import Namespace
from pathlib import Path

import requests
from mutagen import File as MutagenFile
from tqdm import tqdm

from tag_creator.genre_catalog import normalize_genre_name
from tag_creator.matching import plausible_track_match


SOURCE_COLUMNS = ("subgenre", "mood", "moods", "weather", "season", "age_group")
OUTPUT_COLUMNS = (
    "title",
    "album",
    "artist",
    "time",
    "genre",
    "tempo",
    "filename",
    "year",
    "language",
    "isDL",
    "label",
    "vocal",
    "instrumental",
    "tag",
)
DEFAULT_INPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
DEFAULT_SUFFIX = "_with_tag"
DEFAULT_OUTPUT_EXTENSION = ".xls"
SUPPORTED_INPUT_EXTENSIONS = {".csv", ".xls"}
MEDIA_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".aac", ".flac", ".wav", ".wma", ".ogg"}
DEFAULT_ALBUM = "Single"
DEFAULT_LABEL = "SMC"

PROVIDER_ARTIST_COLUMNS = (
    "verified_artist",
    "provider_artist",
    "ai_artist",
    "musicbrainz_artist",
    "spotify_artist",
    "itunes_artist",
    "deezer_artist",
    "lastfm_artist",
    "discogs_artist",
    "acoustid_artist",
    "web_artist",
    "artist_name",
    "artists",
    "track_artist",
    "primary_artist",
)
PROVIDER_JSON_COLUMNS = (
    "merged_json",
    "providers_json",
    "provider_results_json",
    "provider_results",
    "metadata_json",
    "analysis_json",
)
ARTIST_JSON_FIELDS = ("artist", "artists", "artist_name", "track_artist", "primary_artist")
PROVIDER_PRIORITY = {
    "spotify": 1.00,
    "itunes": 0.95,
    "deezer": 0.90,
    "lastfm": 0.85,
    "musicbrainz": 0.80,
    "acoustid": 0.78,
    "discogs": 0.75,
    "web_discovery": 0.70,
    "sonoteller": 0.65,
    "local_ai": 0.60,
    "local_cleanup": 0.20,
}

MOJIBAKE_MARKERS = (
    "\u00c3",
    "\u00c2",
    "\u00e2\u20ac",
    "\u00f0\u0178",
    "\ufffd",
)

PLACEHOLDER_VALUES = {
    "",
    "-",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "unknown language",
    "unknown year",
    "unknown bpm",
    "unknown key",
    "needs_review",
    "needs review",
    "not listed in free sources",
    "not listed",
}

METADATA_IDENTITY_VALUES = {
    "all season",
    "all weather",
    "ambient",
    "blues",
    "classic rock",
    "dance",
    "dance pop",
    "dance-pop",
    "electro",
    "electronic",
    "electronica",
    "electropop",
    "energetic",
    "hard techno",
    "high",
    "house",
    "mainstream",
    "medium",
    "medium high",
    "pop",
    "popular music",
    "retail energy",
    "rock",
    "rock and roll",
    "rock roll",
    "upbeat",
}

TITLE_NOISE_PATTERNS = (
    r"official\s+live\s+video",
    r"official\s+music\s+video",
    r"official\s+video\s+with\s+chords",
    r"official\s+lyric\s+video",
    r"official\s+lyrics\s+video",
    r"official\s+performance\s+video",
    r"official\s+visuali[sz]er",
    r"official\s+audio",
    r"official\s+video",
    r"offizielles?\s+musikvideo",
    r"offizielles?\s+video",
    r"officiell\s+musikvideo",
    r"vid(?:e|\u00e9)o\s+officiel",
    r"officiel(?:le)?\s+video",
    r"audio\s+officiel",
    r"clip\s+officiel",
    r"video\s+oficial",
    r"videoclip\s+oficial",
    r"music\s+video",
    r"lyric\s+video",
    r"lyrics\s+video",
    r"musikvideo",
    r"videoclip",
    "#videosparani\u00f1os",
    r"coke\s+studio",
    r"video\s+edit",
    r"video\s+with\s+fans",
    r"album\s+mix",
    r"4k\s+remaster",
    r"visualizer\s+officiel",
    r"use\s+headphones",
    r"3d\s+audio",
    r"dirty",
    r"clean",
    r"audio",
    r"video",
)
BRACKETED_TITLE_NOISE = re.compile(
    r"\s*[\[(]\s*(?:" + "|".join(TITLE_NOISE_PATTERNS) + r")\s*[\])]\s*",
    flags=re.IGNORECASE,
)
BRACKETED_VIDEO_RESOLUTION = re.compile(
    r"\s*[\[(]\s*(?:480|720|1080|1440|2160)p?\s*[\])]\s*",
    flags=re.IGNORECASE,
)
INLINE_TITLE_NOISE = re.compile(
    r"(?:^|\s+(?:-|–|—|\||/)\s+|\s+)(?:" + "|".join(TITLE_NOISE_PATTERNS) + r")(?=$|\s+(?:-|–|—|\||/)\s+)",
    flags=re.IGNORECASE,
)
TRAILING_TITLE_NOISE = re.compile(
    r"(?:\s*[|]\s*|\s+[-_]\s+|\s+)(?:" + "|".join(TITLE_NOISE_PATTERNS) + r")\s*$",
    flags=re.IGNORECASE,
)
TRAILING_HASHTAGS = re.compile(r"(?:\s+#[\w-]+)+\s*$", flags=re.UNICODE)
FEATURED_ARTIST_TEXT = r"(?:feat\.?|ft\.?|featuring)\b"
BRACKETED_FEATURED_ARTIST = re.compile(
    r"\s*[\[(]\s*" + FEATURED_ARTIST_TEXT + r"[^)\]]*[\])]\s*",
    flags=re.IGNORECASE,
)
TRAILING_FEATURED_ARTIST = re.compile(
    r"\s+" + FEATURED_ARTIST_TEXT + r".*$",
    flags=re.IGNORECASE,
)


def normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in MOJIBAKE_MARKERS)


def contains_mojibake(value: str) -> bool:
    return mojibake_score(clean_value(value)) > 0


def clean_value(value: str) -> str:
    cleaned = unicodedata.normalize("NFC", " ".join(str(value or "").strip().split()))
    # Repair repeated UTF-8/Windows-1252 decoding damage without touching
    # correctly decoded Nordic or other Unicode text.
    for _attempt in range(3):
        current_score = mojibake_score(cleaned)
        if current_score == 0:
            break
        best = cleaned
        best_score = current_score
        for encoding in ("cp1252", "latin-1"):
            try:
                repaired = cleaned.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            repaired_score = mojibake_score(repaired)
            if repaired_score < best_score:
                best = repaired
                best_score = repaired_score
        if best == cleaned:
            break
        cleaned = best
    return unicodedata.normalize("NFC", cleaned)


def remove_icon_characters(value: str) -> str:
    cleaned = clean_value(value)
    characters: list[str] = []
    for character in cleaned:
        category = unicodedata.category(character)
        if character in {"\ufe0e", "\ufe0f", "\u200d"}:
            continue
        if category in {"So", "Sk", "Cs"}:
            continue
        characters.append(character)
    return clean_value("".join(characters))


def clean_title_value(value: str) -> str:
    cleaned = remove_icon_characters(value)
    previous = ""
    while previous != cleaned:
        previous = cleaned
        cleaned = re.sub(r"\s*['\"]{2}\s*", " ", cleaned)
        cleaned = BRACKETED_FEATURED_ARTIST.sub(" ", cleaned)
        cleaned = TRAILING_FEATURED_ARTIST.sub("", cleaned)
        cleaned = BRACKETED_TITLE_NOISE.sub(" ", cleaned)
        cleaned = BRACKETED_VIDEO_RESOLUTION.sub(" ", cleaned)
        cleaned = TRAILING_HASHTAGS.sub("", cleaned)
        cleaned = INLINE_TITLE_NOISE.sub(" ", cleaned)
        cleaned = TRAILING_TITLE_NOISE.sub("", cleaned)
        cleaned = clean_value(cleaned)
    cleaned = re.sub(r"[\[(]\s*[\])]", " ", cleaned)
    cleaned = re.sub(r"\s+([)\]])", r"\1", cleaned)
    cleaned = re.sub(r"([(\[])\s+", r"\1", cleaned)
    return clean_value(cleaned).strip(" -_|'\"")


def normalized_filename_key(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", clean_value(value)).casefold()
    stem = Path(cleaned).stem
    suffix = Path(cleaned).suffix
    stem = "".join(character for character in unicodedata.normalize("NFKD", stem) if not unicodedata.combining(character))
    stem = re.sub(r"[^\w]+", "", stem, flags=re.UNICODE)
    return f"{stem}{suffix}"


def duplicate_filename_key(value: str) -> str:
    """Normalize filename punctuation without collapsing meaningful accents."""

    cleaned = unicodedata.normalize("NFKC", basename_from_value(value)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", cleaned, flags=re.UNICODE).split())


def title_case_value(value: str) -> str:
    cleaned = clean_value(value)
    if not cleaned:
        return ""
    keep_upper = {"ai", "dj", "edm", "r&b", "uk", "us", "usa"}
    words: list[str] = []
    for word in cleaned.split():
        lowered = word.lower()
        words.append(lowered.upper() if lowered in keep_upper else word[:1].upper() + word[1:])
    return " ".join(words)


def canonical_value(value: str) -> str:
    cleaned = clean_value(value).lower().replace("&", "and")
    cleaned = re.sub(r"\bpopular music\b", "pop", cleaned)
    cleaned = re.sub(r"[^a-z0-9/]+", " ", cleaned)
    return " ".join(cleaned.split())


def canonical_title(value: str) -> str:
    cleaned = canonical_value(value)
    cleaned = re.sub(r"\blyrics?\b", " ", cleaned)
    return " ".join(cleaned.split())


def split_tag_values(value: str) -> list[str]:
    cleaned = clean_value(value)
    if not cleaned:
        return []

    # CSV fields already use commas for multi-value tags; semicolon is common in
    # report exports. Keep slash values such as youth/adult intact.
    parts: list[str] = []
    for comma_part in cleaned.split(","):
        parts.extend(comma_part.split(";"))
    return [clean_value(part) for part in parts if clean_value(part)]


def is_usable_tag(value: str) -> bool:
    lowered = clean_value(value).lower()
    normalized = lowered.replace(" ", "_")
    return (
        lowered not in PLACEHOLDER_VALUES
        and not normalized.startswith("needs_review")
        and not is_date_or_year_value(lowered)
        and not is_numeric_metadata_value(lowered)
        and not lowered.startswith(("http://", "https://"))
        and not re.fullmatch(r"[a-g](?:#|b)?\s+(?:major|minor)", lowered)
    )


def is_missing_identity(value: str) -> bool:
    cleaned = clean_value(value).lower()
    normalized = cleaned.replace(" ", "_")
    return (
        not cleaned
        or cleaned in PLACEHOLDER_VALUES
        or cleaned.startswith("unknown")
        or cleaned.startswith("not listed")
        or normalized.startswith("needs_review")
    )


def is_date_or_year_value(value: str) -> bool:
    cleaned = clean_value(value)
    if re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?", cleaned):
        return True
    return bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", cleaned))


def is_numeric_metadata_value(value: str) -> bool:
    cleaned = clean_value(value)
    if not cleaned:
        return False
    try:
        float(cleaned)
    except ValueError:
        return False
    return True


def is_unverified_title(value: str) -> bool:
    cleaned = clean_value(value)
    return (
        is_missing_identity(cleaned)
        or is_date_or_year_value(cleaned)
        or is_numeric_metadata_value(cleaned)
        or cleaned.lower().startswith(("http://", "https://"))
    )


def basename_from_value(value: str) -> str:
    cleaned = clean_value(value).replace("\\", "/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def strip_media_extension(value: str) -> str:
    name = basename_from_value(value)
    suffix = Path(name).suffix
    return clean_value(name[: -len(suffix)] if suffix else name)


def standardize_lyrics_marker(value: str) -> str:
    cleaned = clean_value(value)
    if not cleaned:
        return ""
    has_lyrics = bool(re.search(r"[\[\(]\s*lyrics?\s*[\]\)]|\blyrics?\b", cleaned, flags=re.I))
    cleaned = re.sub(r"[\[\(]\s*lyrics?\s*[\]\)]", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\blyrics?\b", " ", cleaned, flags=re.I)
    cleaned = clean_value(cleaned).strip(" -_")
    return f"{cleaned} (Lyrics)" if has_lyrics and cleaned else cleaned


def has_lyrics_marker(*values: str) -> bool:
    return any(re.search(r"[\[\(]\s*lyrics?\s*[\]\)]|\blyrics?\b", value or "", flags=re.I) for value in values)


def parse_artist_title_from_filename(filename: str) -> tuple[str, str]:
    stem = strip_media_extension(filename)
    if not stem:
        return "", ""

    # Accept the separators normally used by download tools without embedding
    # locale-dependent dash characters in the source file.
    for match in re.finditer(r"\s+(?:-|\u2013|\u2014)\s+", stem):
        artist = stem[: match.start()]
        title = stem[match.end() :]
        if clean_value(artist) and clean_value(title):
            return clean_value(artist), standardize_lyrics_marker(title)
    return "", ""


def parse_artist_title_from_text(value: str) -> tuple[str, str]:
    cleaned = clean_value(value)
    if not cleaned:
        return "", ""
    for match in re.finditer(r"\s+(?:-|\u2013|\u2014)\s+", cleaned):
        artist = cleaned[: match.start()]
        title = cleaned[match.end() :]
        if clean_value(artist) and clean_value(title):
            return clean_value(artist), standardize_lyrics_marker(title)
    return "", ""


def title_without_artist_prefix(title: str, *artists: str) -> str:
    parsed_artist, parsed_title = parse_artist_title_from_text(title)
    if not parsed_artist or not parsed_title:
        return title

    parsed_artist_key = canonical_value(parsed_artist)
    artist_keys = {canonical_value(artist) for artist in artists if artist}
    if artist_keys and parsed_artist_key not in artist_keys:
        return title
    return parsed_title


def filename_for_row(row: dict[str, str], header_map: dict[str, str]) -> str:
    return row_value(row, header_map, "filename") or basename_from_value(row_value(row, header_map, "file_path", "path"))


def fallback_from_filename(filename: str, field: str) -> str:
    artist, title = parse_artist_title_from_filename(filename)
    if field == "artist":
        return artist
    return title


def is_unverified_artist(value: str) -> bool:
    return is_missing_identity(value) or canonical_value(value) in {"smc", "tag creator"} | METADATA_IDENTITY_VALUES


def is_unverified_genre(value: str) -> bool:
    cleaned = clean_value(value)
    return (
        is_missing_identity(cleaned)
        or is_date_or_year_value(cleaned)
        or is_numeric_metadata_value(cleaned)
        or cleaned.lower().startswith(("http://", "https://"))
    )


def is_unverified_language(value: str) -> bool:
    cleaned = clean_value(value)
    return (
        is_missing_identity(cleaned)
        or is_date_or_year_value(cleaned)
        or is_numeric_metadata_value(cleaned)
        or canonical_value(cleaned) in METADATA_IDENTITY_VALUES
        or cleaned.lower().startswith(("http://", "https://"))
    )


def genre_from_path(row: dict[str, str], header_map: dict[str, str]) -> str:
    raw_path = row_value(row, header_map, "file_path", "path")
    if not raw_path:
        return ""
    normalized = raw_path.replace("\\", "/").strip("/")
    for marker in ("app/input_media/", "app/mp3/", "app/mp4/", "app/media/"):
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
            break
    parts = [clean_value(part) for part in normalized.split("/")[:-1] if clean_value(part)]
    ignored = {"input_media", "media", "mp3", "mp4", "normalized"}
    month_names = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december"
    for part in parts:
        key = canonical_value(part.replace("(new)", ""))
        if not key or key in ignored:
            continue
        if re.fullmatch(r"\d{2,4}s?", key) or re.fullmatch(rf"(?:{month_names})\s+\d{{4}}", key):
            continue
        genre = normalize_genre_name(part) or title_case_value(part)
        if genre and not is_unverified_genre(genre):
            return genre
    return ""


def resolve_single_genre(
    row: dict[str, str],
    header_map: dict[str, str],
    *,
    resolved_title: str = "",
    resolved_artist: str = "",
) -> str:
    raw_genre = row_value(row, header_map, "genre")
    candidates = split_tag_values(raw_genre)
    primary = candidates[0] if candidates else raw_genre
    normalized = normalize_genre_name(primary)
    normalized_parts = split_tag_values(normalized)
    genre = normalized_parts[0] if normalized_parts else title_case_value(primary)
    identity_keys = {canonical_title(resolved_title), canonical_value(resolved_artist)}
    if genre and not is_unverified_genre(genre) and canonical_value(genre) not in identity_keys:
        return genre
    path_genre = genre_from_path(row, header_map)
    if path_genre and canonical_value(path_genre) not in identity_keys:
        return path_genre
    return ""


def normalize_tag_value(value: str, source_column: str) -> str:
    # Only genre-like values should be mapped against the portal genre API.
    # Mood, weather, season and audience tags are separate taxonomies.
    if source_column == "subgenre":
        normalized = normalize_genre_name(value)
        return normalized or title_case_value(value)
    return title_case_value(value)


def text_from_metadata_value(value: object) -> str:
    if isinstance(value, str):
        return clean_value(value)
    if isinstance(value, (int, float)):
        return clean_value(str(value))
    if isinstance(value, list):
        parts = [text_from_metadata_value(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("name", "artist", "artist_name", "title"):
            if key in value:
                text = text_from_metadata_value(value.get(key))
                if text:
                    return text
    return ""


def normalize_artist_candidate(value: object, *, title: str = "", parsed_title: str = "") -> str:
    candidate = text_from_metadata_value(value).strip(" -_|'\"")
    if not candidate:
        return ""

    parsed_artist, candidate_title = parse_artist_title_from_filename(candidate)
    title_keys = {canonical_title(title), canonical_title(parsed_title)}
    if parsed_artist and candidate_title and canonical_title(candidate_title) in title_keys:
        candidate = parsed_artist

    if is_unverified_artist(candidate):
        return ""
    candidate_key = canonical_value(candidate)
    if not candidate_key or candidate_key in title_keys:
        return ""
    if len(candidate) > 140:
        return ""
    return candidate


def float_from_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def artist_candidates_from_json(
    value: str,
    *,
    title: str = "",
    parsed_title: str = "",
) -> list[tuple[float, str]]:
    cleaned = clean_value(value)
    if not cleaned:
        return []
    try:
        data = json.loads(cleaned)
    except ValueError:
        return []

    candidates: list[tuple[float, str]] = []

    def add_candidate(candidate_value: object, score: float) -> None:
        candidate = normalize_artist_candidate(candidate_value, title=title, parsed_title=parsed_title)
        if candidate:
            candidates.append((score, candidate))

    def walk(node: object, base_score: float = 0.0, provider: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, base_score, provider)
            return
        if not isinstance(node, dict):
            return

        provider_name = clean_value(str(node.get("provider") or provider)).lower()
        confidence = float_from_value(node.get("confidence"), 0.0)
        provider_score = PROVIDER_PRIORITY.get(provider_name, 0.50)

        fields = node.get("fields")
        if isinstance(fields, dict):
            field_confidence = node.get("field_confidence")
            artist_confidence = (
                float_from_value(field_confidence.get("artist"), confidence)
                if isinstance(field_confidence, dict)
                else confidence
            )
            for field in ARTIST_JSON_FIELDS:
                if field in fields:
                    add_candidate(fields.get(field), 5.0 + provider_score + artist_confidence)

        for field in ARTIST_JSON_FIELDS:
            if field in node:
                add_candidate(node.get(field), base_score + 3.0 + provider_score + confidence)

        merged = node.get("merged")
        if isinstance(merged, dict):
            walk(merged, base_score + 0.5, provider_name)

    walk(data)
    return candidates


def provider_artist_from_row(
    row: dict[str, str],
    header_map: dict[str, str],
    *,
    title: str = "",
    parsed_title: str = "",
) -> str:
    candidates: list[tuple[float, str]] = []
    for index, column in enumerate(PROVIDER_ARTIST_COLUMNS):
        value = row_value(row, header_map, column)
        candidate = normalize_artist_candidate(value, title=title, parsed_title=parsed_title)
        if candidate:
            candidates.append((4.0 - (index * 0.01), candidate))

    for column in PROVIDER_JSON_COLUMNS:
        value = row_value(row, header_map, column)
        candidates.extend(artist_candidates_from_json(value, title=title, parsed_title=parsed_title))

    deduped: dict[str, tuple[float, str]] = {}
    for score, candidate in candidates:
        key = canonical_value(candidate)
        if not key:
            continue
        current = deduped.get(key)
        if current is None or score > current[0]:
            deduped[key] = (score, candidate)

    if not deduped:
        return ""
    return max(deduped.values(), key=lambda item: item[0])[1]


def resolve_artist(
    row: dict[str, str],
    header_map: dict[str, str],
    duration_resolver: MediaDurationResolver | None = None,
    csv_context: str = "",
) -> str:
    artist = row_value(row, header_map, "artist")
    filename = filename_for_row(row, header_map)
    parsed_artist, parsed_title = parse_artist_title_from_filename(filename)
    title = standardize_lyrics_marker(row_value(row, header_map, "title"))
    should_replace_from_identity = (
        is_unverified_artist(artist)
        or is_unverified_title(title)
        or (parsed_title and title and canonical_title(parsed_title) == canonical_title(title))
    )
    if not should_replace_from_identity and not is_unverified_artist(artist):
        return artist

    provider_artist = provider_artist_from_row(row, header_map, title=title, parsed_title=parsed_title)
    if provider_artist:
        return provider_artist

    if duration_resolver:
        embedded_artist = duration_resolver.resolve_embedded_identity(
            row,
            header_map,
            "artist",
            csv_context,
        )
        if not is_unverified_artist(embedded_artist):
            return embedded_artist
    if parsed_artist and should_replace_from_identity:
        return parsed_artist
    if not is_unverified_artist(artist):
        return artist
    return ""


def resolve_title(
    row: dict[str, str],
    header_map: dict[str, str],
    duration_resolver: MediaDurationResolver | None = None,
    csv_context: str = "",
) -> str:
    filename = filename_for_row(row, header_map)
    row_artist = row_value(row, header_map, "artist")
    parsed_artist, parsed_title = parse_artist_title_from_filename(filename)
    title = standardize_lyrics_marker(row_value(row, header_map, "title"))
    title = title_without_artist_prefix(title, row_artist, parsed_artist)
    title = clean_title_value(title)
    if parsed_title and (
        is_unverified_title(title)
        or has_lyrics_marker(filename, parsed_title)
        or canonical_value(parsed_title).replace(" lyrics", "") == canonical_value(title)
    ):
        return clean_title_value(parsed_title)
    if not is_unverified_title(title):
        return f"{title} (Lyrics)" if has_lyrics_marker(filename) and "(lyrics)" not in title.lower() else title
    if duration_resolver:
        embedded_title = duration_resolver.resolve_embedded_identity(
            row,
            header_map,
            "title",
            csv_context,
        )
        if not is_unverified_title(embedded_title):
            return clean_title_value(standardize_lyrics_marker(embedded_title))
    return clean_title_value(fallback_from_filename(filename, "title"))


def build_tag(
    row: dict[str, str],
    header_map: dict[str, str],
    *,
    resolved_title: str = "",
    resolved_artist: str = "",
    resolved_genre: str = "",
) -> str:
    tags: list[str] = []
    seen: set[str] = set()
    genre_key = canonical_value(resolved_genre or row_value(row, header_map, "genre"))
    identity_keys = {
        canonical_value(value)
        for value in (
            resolved_title,
            resolved_artist,
            row_value(row, header_map, "album"),
            row_value(row, header_map, "album_artist"),
        )
        if value and not is_missing_identity(value)
    }

    for source_column in SOURCE_COLUMNS:
        actual_header = header_map.get(source_column)
        if not actual_header:
            continue
        for value in split_tag_values(row.get(actual_header, "")):
            if not is_usable_tag(value):
                continue
            final_value = normalize_tag_value(value, source_column)
            key = canonical_value(final_value)
            if not key or key == "general":
                continue
            if genre_key and key == genre_key:
                continue
            if key in identity_keys:
                continue
            if key in seen:
                continue
            seen.add(key)
            tags.append(final_value)

    return ",".join(tags)


def row_value(row: dict[str, str], header_map: dict[str, str], *candidates: str) -> str:
    for candidate in candidates:
        actual_header = header_map.get(normalize_header(candidate))
        if actual_header:
            value = clean_value(row.get(actual_header, ""))
            if value:
                return value
    return ""


def format_time(value: str) -> str:
    cleaned = clean_value(value)
    if cleaned.startswith('="') and cleaned.endswith('"'):
        cleaned = cleaned[2:-1]
    if not cleaned:
        return "00:00:00"

    if ":" in cleaned:
        parts = [part.strip() for part in cleaned.split(":") if part.strip()]
        try:
            numbers = [int(float(part)) for part in parts]
        except ValueError:
            return "00:00:00"
        if len(numbers) == 3:
            hours, minutes, seconds = numbers
        elif len(numbers) == 2:
            hours, minutes, seconds = 0, numbers[0], numbers[1]
        elif len(numbers) == 1:
            hours, minutes, seconds = 0, 0, numbers[0]
        else:
            return "00:00:00"
    else:
        try:
            total_seconds = int(round(float(cleaned)))
        except ValueError:
            return "00:00:00"
        hours, remainder = divmod(max(total_seconds, 0), 3600)
        minutes, seconds = divmod(remainder, 60)

    total_seconds = max(0, hours * 3600 + minutes * 60 + seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def time_to_seconds(value: str) -> int:
    formatted = format_time(value)
    try:
        hours, minutes, seconds = (int(part) for part in formatted.split(":"))
    except (TypeError, ValueError):
        return 0
    return max(0, hours * 3600 + minutes * 60 + seconds)


LANGUAGE_NAMES = {
    "da": "Danish",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "is": "Icelandic",
    "it": "Italian",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "sv": "Swedish",
    "tr": "Turkish",
}


class MissingLanguageResolver:
    """Resolve only blank/placeholder language values from textual evidence."""

    def __init__(self, *, enabled: bool = True, session: requests.Session | None = None) -> None:
        self.enabled = enabled
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            os.getenv("LANGUAGE_LOOKUP_USER_AGENT", "SMC-Tag-Creator/0.1 (tanjim@advikon.eu)"),
        )
        self.timeout = max(3, int(os.getenv("LANGUAGE_LOOKUP_TIMEOUT_SECONDS", "15")))
        self._cache: dict[tuple[str, str, str, str], str] = {}

    def resolve(self, existing: str, *, title: str, artist: str, album: str, csv_context: str) -> str:
        cleaned = clean_value(existing)
        if cleaned and not is_unverified_language(cleaned):
            return cleaned
        if not self.enabled:
            return ""

        cache_key = (
            canonical_title(title),
            canonical_value(artist),
            canonical_value(album),
            canonical_value(csv_context),
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        language = self._language_from_lrclib(title, artist, csv_context)
        if not language:
            language = self._language_from_identity_text(title, album, csv_context)
        self._cache[cache_key] = language
        return language

    def _language_from_lrclib(self, title: str, artist: str, csv_context: str) -> str:
        if not title:
            return ""
        params = {"track_name": title}
        if artist and not is_missing_identity(artist):
            params["artist_name"] = artist

        for attempt in range(2):
            try:
                response = self.session.get(
                    "https://lrclib.net/api/search",
                    params=params,
                    timeout=(5, self.timeout),
                )
            except requests.RequestException:
                return ""
            if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(1.0)
                continue
            if not response.ok:
                return ""
            try:
                candidates = response.json()
            except ValueError:
                return ""
            if not isinstance(candidates, list):
                return ""

            ranked: list[tuple[float, str]] = []
            for candidate in candidates[:10]:
                if not isinstance(candidate, dict):
                    continue
                candidate_title = clean_value(candidate.get("trackName") or candidate.get("name") or "")
                candidate_artist = clean_value(candidate.get("artistName") or "")
                plausible, title_score, artist_score = plausible_track_match(
                    title,
                    "" if is_missing_identity(artist) else artist,
                    candidate_title,
                    candidate_artist,
                    min_title=0.72,
                    min_artist=0.50,
                )
                lyrics = clean_value(candidate.get("plainLyrics") or candidate.get("syncedLyrics") or "")
                if plausible and lyrics:
                    ranked.append(((title_score * 0.65) + (artist_score * 0.35), lyrics))
            if not ranked:
                return ""
            lyrics = max(ranked, key=lambda item: item[0])[1]
            detected = self._detect_text_language(lyrics, min_characters=120, min_confidence=0.80)
            return self._refine_nordic_language(lyrics, detected, csv_context)
        return ""

    @staticmethod
    def _refine_nordic_language(text: str, detected: str, csv_context: str) -> str:
        """Correct common Danish/Norwegian/Swedish detector confusion from lexical evidence."""
        tokens = set(re.findall(r"[a-zà-ÿ]+", clean_value(text).casefold()))
        danish_words = {
            "arbejdsplads", "behøver", "dæmoner", "dig", "græder", "hvad",
            "jer", "kærlighed", "køre", "kvinde", "ligesom", "mænd", "mig",
            "nogen", "noget", "sådan", "smukkest", "stadig", "tænker", "uden",
        }
        norwegian_words = {
            "arbeidsplass", "deg", "dere", "fortsatt", "gråter", "hva",
            "kjærlighet", "kjøre", "kvinne", "liksom", "meg", "menn", "noe",
            "noen", "sånn", "tenker", "uten", "våre",
        }
        swedish_words = {
            "aldrig", "att", "dig", "eftersom", "från", "inte", "jag", "kvinna",
            "men", "mig", "någon", "något", "och", "ska", "tänker", "utan",
        }
        danish_score = len(tokens & danish_words)
        norwegian_score = len(tokens & norwegian_words)
        swedish_score = len(tokens & swedish_words)

        # Full lyrics need several independent Danish markers. For a Denmark
        # catalog, two markers are sufficient supporting evidence because the
        # track identity was already verified before lyric analysis.
        required_score = 2 if "denmark" in canonical_value(csv_context) else 3
        if danish_score >= required_score and danish_score > max(norwegian_score, swedish_score):
            return "Danish"
        return detected

    @staticmethod
    def _detect_text_language(text: str, *, min_characters: int, min_confidence: float) -> str:
        sample = re.sub(r"\[[^\]]*\]", " ", clean_value(text))
        sample = re.sub(r"\s+", " ", sample).strip()[:12000]
        if len(sample) < min_characters:
            return ""
        try:
            from langdetect import DetectorFactory, detect_langs
        except ImportError:
            return ""
        DetectorFactory.seed = 0
        try:
            predictions = detect_langs(sample)
        except Exception:  # langdetect raises several errors for unsuitable text
            return ""
        if not predictions:
            return ""
        best = predictions[0]
        code = str(getattr(best, "lang", "")).lower()
        probability = float(getattr(best, "prob", 0.0))
        return LANGUAGE_NAMES.get(code, "") if probability >= min_confidence else ""

    @classmethod
    def _language_from_identity_text(cls, title: str, album: str, csv_context: str) -> str:
        text = clean_value(" ".join(value for value in (title, album) if value))
        # Short titles are difficult for statistical detectors. These markers
        # are accepted only with matching Denmark context and clear Danish text.
        context = canonical_value(csv_context)
        lowered = f" {text.casefold()} "
        danish_markers = (
            "kærlighed", "dæmon", "sådan", "nogen", "mænd", "græder",
            "søndag", "feberdrøm", "ødemark", " øde ", " gå ", " dig ",
        )
        if "denmark" in context and (
            "æ" in lowered or sum(marker in lowered for marker in danish_markers) >= 1
        ):
            return "Danish"

        detected = cls._detect_text_language(text, min_characters=18, min_confidence=0.95)
        return cls._refine_nordic_language(text, detected, csv_context)


class MediaDurationResolver:
    def __init__(self, media_roots: list[Path], *, verify_vocals: bool | None = None) -> None:
        self.media_roots = [root for root in media_roots if root.exists()]
        self.verify_vocals = (
            verify_vocals
            if verify_vocals is not None
            else os.getenv("VERIFY_VOCAL_FLAGS", "true").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._name_index: dict[str, list[Path]] | None = None
        self._normalized_name_index: dict[str, list[Path]] | None = None
        self._relative_index: dict[str, Path] | None = None
        self._duration_cache: dict[Path, str] = {}
        self._audio_fact_cache: dict[Path, dict[str, str]] = {}
        self._embedded_identity_cache: dict[Path, dict[str, str]] = {}

    def has_media_roots(self) -> bool:
        return bool(self.media_roots)

    def resolve_time(
        self,
        row: dict[str, str],
        header_map: dict[str, str],
        existing_value: str,
        csv_context: str = "",
    ) -> str:
        formatted = format_time(existing_value)
        if formatted != "00:00:00":
            return formatted

        media_path = self.resolve_media_path(row, header_map, csv_context)
        if not media_path:
            return formatted
        return self._duration_for_file(media_path) or formatted

    def is_downloaded(self, row: dict[str, str], header_map: dict[str, str], csv_context: str = "") -> str:
        return "1" if self.resolve_media_path(row, header_map, csv_context) else "0"

    def resolve_embedded_identity(
        self,
        row: dict[str, str],
        header_map: dict[str, str],
        field: str,
        csv_context: str = "",
    ) -> str:
        media_path = self.resolve_media_path(row, header_map, csv_context)
        if not media_path:
            return ""
        return self._embedded_identity_for_file(media_path).get(field, "")

    def _embedded_identity_for_file(self, path: Path) -> dict[str, str]:
        cached = self._embedded_identity_cache.get(path)
        if cached is not None:
            return cached

        identity: dict[str, str] = {}
        try:
            media = MutagenFile(path, easy=True)
            tags = getattr(media, "tags", None) or {}
            for field, candidates in {
                "artist": ("artist", "albumartist", "performer"),
                "title": ("title",),
            }.items():
                for candidate in candidates:
                    values = tags.get(candidate, []) if hasattr(tags, "get") else []
                    if isinstance(values, str):
                        values = [values]
                    value = clean_value(values[0]) if values else ""
                    if value and not is_missing_identity(value):
                        identity[field] = value
                        break
        except Exception:
            identity = {}
        self._embedded_identity_cache[path] = identity
        return identity

    def resolve_media_path(self, row: dict[str, str], header_map: dict[str, str], csv_context: str = "") -> Path | None:
        raw_file_path = row_value(row, header_map, "file_path", "path")
        filename = row_value(row, header_map, "filename")
        if raw_file_path:
            declared_path = self._resolve_declared_file_path(raw_file_path, csv_context)
            if declared_path:
                return declared_path

        raw_path = Path(raw_file_path) if raw_file_path else None
        relative_parts = self._relative_candidates(raw_file_path, filename, csv_context)

        candidates: list[Path] = []
        if raw_path:
            candidates.append(raw_path)
            for root in self.media_roots:
                candidates.append(root / raw_path.name)
        if filename:
            for root in self.media_roots:
                candidates.append(root / filename)
                if csv_context:
                    candidates.append(root / csv_context / filename)
        for relative in relative_parts:
            for root in self.media_roots:
                candidates.append(root / relative)

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate

        for relative in relative_parts:
            match = self._relative_media_index().get(self._index_key(relative))
            if match:
                return match

        basename = filename or (raw_path.name if raw_path else "")
        if basename and self.media_roots:
            return self._unique_name_match(basename, csv_context)
        return None

    @staticmethod
    def _index_key(path: Path | str) -> str:
        return str(path).replace("\\", "/").casefold().strip("/")

    @staticmethod
    def _strip_app_mount(raw_path: str) -> Path | None:
        normalized = raw_path.replace("\\", "/")
        for marker in ("/app/input_media/", "/app/mp3/", "/app/mp4/", "/app/media/"):
            if marker in normalized:
                tail = normalized.split(marker, 1)[1].strip("/")
                return Path(*tail.split("/")) if tail else None
        return None

    @staticmethod
    def _context_stripped_relative_path(relative_path: Path, csv_context: str) -> Path | None:
        if not csv_context:
            return None
        parts = relative_path.parts
        if len(parts) <= 1:
            return None
        if clean_value(parts[0]).casefold() != clean_value(csv_context).casefold():
            return None
        return Path(*parts[1:])

    def _relative_path_candidates(self, relative_path: Path, csv_context: str = "") -> list[Path]:
        candidates = [relative_path]
        stripped = self._context_stripped_relative_path(relative_path, csv_context)
        if stripped:
            candidates.append(stripped)
        return candidates

    def _resolve_declared_file_path(self, raw_file_path: str, csv_context: str = "") -> Path | None:
        declared = self._declared_relative_path(raw_file_path)
        if declared:
            return self._resolve_relative_media_path(declared, csv_context)

        raw_path = Path(raw_file_path)
        if raw_path.is_absolute():
            return raw_path if raw_path.exists() and raw_path.is_file() else None

        return self._resolve_relative_media_path(raw_path, csv_context)

    def _resolve_relative_media_path(self, relative_path: Path, csv_context: str = "") -> Path | None:
        for candidate_relative in self._relative_path_candidates(relative_path, csv_context):
            for root in self.media_roots:
                candidate = root / candidate_relative
                if candidate.exists() and candidate.is_file():
                    return candidate
            match = self._relative_media_index().get(self._index_key(candidate_relative))
            if match:
                return match
        return None

    @classmethod
    def _declared_relative_path(cls, raw_file_path: str) -> Path | None:
        if not raw_file_path:
            return None
        stripped = cls._strip_app_mount(raw_file_path)
        if stripped:
            return stripped
        raw_path = Path(raw_file_path)
        if not raw_path.is_absolute() and raw_path.parent != Path("."):
            return raw_path
        return None

    def _relative_candidates(self, raw_file_path: str, filename: str, csv_context: str) -> list[Path]:
        candidates: list[Path] = []
        if raw_file_path:
            stripped = self._strip_app_mount(raw_file_path)
            if stripped:
                candidates.extend(self._relative_path_candidates(stripped, csv_context))
                if csv_context and stripped.name == str(stripped):
                    candidates.append(Path(csv_context) / stripped.name)
        if filename and csv_context:
            candidates.append(Path(csv_context) / filename)

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = self._index_key(candidate)
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return unique

    def _scan_indexes(self) -> None:
        if self._name_index is not None and self._normalized_name_index is not None and self._relative_index is not None:
            return
        name_index: dict[str, list[Path]] = {}
        normalized_name_index: dict[str, list[Path]] = {}
        relative_index: dict[str, Path] = {}
        for root in self.media_roots:
            for candidate in root.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTENSIONS:
                    name_index.setdefault(candidate.name.casefold(), []).append(candidate)
                    normalized_name_index.setdefault(normalized_filename_key(candidate.name), []).append(candidate)
                    try:
                        relative = candidate.relative_to(root)
                    except ValueError:
                        relative = Path(candidate.name)
                    relative_index.setdefault(self._index_key(relative), candidate)
        self._name_index = name_index
        self._normalized_name_index = normalized_name_index
        self._relative_index = relative_index

    def _relative_media_index(self) -> dict[str, Path]:
        self._scan_indexes()
        return self._relative_index or {}

    def _name_media_index(self) -> dict[str, list[Path]]:
        self._scan_indexes()
        return self._name_index or {}

    def _unique_name_match(self, filename: str, csv_context: str) -> Path | None:
        matches = self._name_media_index().get(filename.casefold(), [])
        if not matches:
            self._scan_indexes()
            matches = (self._normalized_name_index or {}).get(normalized_filename_key(filename), [])
        if not matches:
            return None
        if csv_context:
            context_key = f"/{csv_context.casefold()}/"
            context_matches = [path for path in matches if context_key in path.as_posix().casefold()]
            if len(context_matches) == 1:
                return context_matches[0]
        return matches[0] if len(matches) == 1 else None

    def _duration_for_file(self, path: Path) -> str:
        cached = self._duration_cache.get(path)
        if cached is not None:
            return cached
        try:
            media = MutagenFile(path)
            length = getattr(getattr(media, "info", None), "length", None)
            duration = format_time(str(length)) if length else ""
        except Exception:
            duration = ""
        if not duration or duration == "00:00:00":
            duration = self._duration_with_ffprobe(path)
        self._duration_cache[path] = duration
        return duration

    def resolve_bpm(
        self,
        row: dict[str, str],
        header_map: dict[str, str],
        existing_value: str,
        csv_context: str = "",
    ) -> str:
        numeric = self._numeric_bpm(existing_value)
        if numeric:
            return numeric
        media_path = self.resolve_media_path(row, header_map, csv_context)
        if not media_path:
            return ""
        return self._audio_facts(media_path).get("bpm", "")

    def resolve_vocal_flags(
        self,
        row: dict[str, str],
        header_map: dict[str, str],
        vocals: str,
        instrumental: str,
        csv_context: str = "",
    ) -> tuple[str, str]:
        media_path = self.resolve_media_path(row, header_map, csv_context)
        if self.verify_vocals and media_path:
            facts = self._audio_facts(media_path)
            if facts.get("vocal") in {"0", "1"}:
                return facts["vocal"], facts["instrumental"]
            # Do not preserve a generic source fallback as an audio fact.
            # Validation reports the unresolved pair while retaining the row.
            return "", ""

        # Verification can be disabled explicitly for legacy conversions that
        # have no mounted media. In that mode preserve a valid source pair.
        if clean_value(vocals) or clean_value(instrumental):
            instrumental_flag = boolean_flag(
                instrumental or vocals,
                truthy_words=("instrumental", "no vocal", "non vocal"),
            )
            vocal_flag = boolean_flag(vocals, truthy_words=("vocal", "voice", "sing"), default="0")
            if instrumental_flag == "1":
                return "0", "1"
            if vocal_flag == "1":
                return "1", "0"

        if media_path:
            facts = self._audio_facts(media_path)
            if facts.get("vocal") in {"0", "1"}:
                return facts["vocal"], facts["instrumental"]
        return "", ""

    @staticmethod
    def _numeric_bpm(value: str) -> str:
        cleaned = clean_value(value)
        try:
            bpm = float(cleaned)
        except ValueError:
            return ""
        return str(int(round(bpm))) if 30 <= bpm <= 300 else ""

    @staticmethod
    def _classify_vocal_tags(
        ranked: list[dict[str, object]],
        *,
        voice_labels: set[str],
        min_confidence: float,
        min_margin: float,
    ) -> dict[str, str]:
        scores: dict[str, float] = {}
        for item in ranked:
            label = clean_value(str(item.get("label", ""))).casefold()
            if label in voice_labels:
                canonical_label = "voice"
            elif label == "instrumental":
                canonical_label = "instrumental"
            else:
                raise ValueError(f"unexpected voice/instrumental label: {label or '<blank>'}")
            scores[canonical_label] = max(scores.get(canonical_label, 0.0), float(item.get("score", 0.0)))

        if set(scores) != {"voice", "instrumental"}:
            raise ValueError("voice/instrumental classifier did not return both expected classes")
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_label, top_score = ordered[0]
        second_score = ordered[1][1]
        if top_score < min_confidence or (top_score - second_score) < min_margin:
            raise ValueError(f"low voice/instrumental confidence: {top_score:.3f}/{second_score:.3f}")
        is_instrumental = top_label == "instrumental"
        return {
            "vocal": "0" if is_instrumental else "1",
            "instrumental": "1" if is_instrumental else "0",
            "vocal_confidence": f"{top_score:.3f}",
        }

    def _audio_facts(self, path: Path) -> dict[str, str]:
        cached = self._audio_fact_cache.get(path)
        if cached is not None:
            return cached

        facts: dict[str, str] = {}
        errors: list[str] = []
        analysis_path, temporary_path = self._analysis_preview(path)
        try:
            try:
                from tag_creator.local_ai_runner import (
                    run_clap_zero_shot,
                    run_essentia_features,
                    run_essentia_voice_instrumental,
                )
            except ImportError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            else:
                try:
                    feature_result = run_essentia_features(Namespace(audio=analysis_path))
                    bpm = self._numeric_bpm(str((feature_result.get("features") or {}).get("bpm", "")))
                    if bpm:
                        facts["bpm"] = bpm
                except Exception as exc:  # noqa: BLE001 - one media failure must not stop the batch
                    errors.append(f"BPM {type(exc).__name__}: {exc}")

                model_dir = Path(os.getenv("LOCAL_AI_MODELS_DIR", "models/local_ai"))
                embedding_model = Path(
                    os.getenv(
                        "ESSENTIA_DISCOGS_EMBEDDING_MODEL",
                        str(model_dir / "discogs-effnet-embeddings.pb"),
                    )
                )
                voice_model = Path(
                    os.getenv(
                        "ESSENTIA_VOICE_INSTRUMENTAL_MODEL",
                        str(model_dir / "voice_instrumental-discogs-effnet.pb"),
                    )
                )
                voice_labels = Path(
                    os.getenv(
                        "ESSENTIA_VOICE_INSTRUMENTAL_LABELS",
                        str(model_dir / "voice_instrumental-labels.txt"),
                    )
                )
                try:
                    if not all(path.exists() for path in (embedding_model, voice_model, voice_labels)):
                        raise FileNotFoundError("dedicated voice/instrumental model files are missing")
                    voice_result = run_essentia_voice_instrumental(
                        Namespace(
                            audio=analysis_path,
                            embedding_model=embedding_model,
                            prediction_model=voice_model,
                            labels=voice_labels,
                            output_node="model/Softmax",
                        )
                    )
                    ranked = voice_result.get("tags", [])
                    min_confidence = float(os.getenv("VOCAL_MIN_CONFIDENCE", "0.60"))
                    min_margin = float(os.getenv("VOCAL_MIN_MARGIN", "0.10"))
                    facts.update(
                        self._classify_vocal_tags(
                            ranked,
                            voice_labels={"voice"},
                            min_confidence=min_confidence,
                            min_margin=min_margin,
                        )
                    )
                    facts["vocal_source"] = "essentia_voice_instrumental"
                except Exception as exc:  # noqa: BLE001 - CLAP remains a measured fallback
                    errors.append(f"dedicated vocals {type(exc).__name__}: {exc}")

                if "vocal" not in facts:
                    try:
                        clap_result = run_clap_zero_shot(
                        Namespace(
                            audio=analysis_path,
                            model_name=os.getenv("CLAP_MODEL_NAME", "laion/clap-htsat-unfused"),
                            cache_dir=Path(os.getenv("CLAP_CACHE_DIR", "models/local_ai/hf")),
                            label=["vocals: vocal", "vocals: instrumental"],
                            top_n=2,
                            max_seconds=max(15, int(os.getenv("CLAP_MAX_SECONDS", "45"))),
                        )
                        )
                        ranked = [tag for tag in clap_result.get("tags", []) if tag.get("field") == "vocals"]
                        min_margin = float(os.getenv("CLAP_VOCAL_MIN_MARGIN", "0.02"))
                        facts.update(
                            self._classify_vocal_tags(
                                ranked,
                                voice_labels={"vocal"},
                                min_confidence=0.0,
                                min_margin=min_margin,
                            )
                        )
                        facts["vocal_source"] = "clap_zero_shot"
                    except Exception as exc:  # noqa: BLE001 - preserve independently measured BPM
                        errors.append(f"CLAP vocals {type(exc).__name__}: {exc}")
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

        if errors:
            facts["analysis_error"] = "; ".join(errors)

        self._audio_fact_cache[path] = facts
        return facts

    @staticmethod
    def _analysis_segments(total_seconds: int) -> list[tuple[int, int]]:
        if total_seconds <= 0:
            return [(0, 45)]
        if total_seconds <= 90:
            return [(0, total_seconds)]

        segment_seconds = 15
        half_segment = segment_seconds // 2
        centers = (0.15, 0.50, 0.85)
        return [
            (
                max(0, min(total_seconds - segment_seconds, int(total_seconds * center) - half_segment)),
                segment_seconds,
            )
            for center in centers
        ]

    def _analysis_preview(self, path: Path) -> tuple[Path, Path | None]:
        duration = self._duration_for_file(path)
        total_seconds = time_to_seconds(duration)
        segments = self._analysis_segments(total_seconds)
        handle = tempfile.NamedTemporaryFile(prefix="tag_creator_final_", suffix=".wav", delete=False)
        preview_path = Path(handle.name)
        handle.close()
        inputs: list[str] = []
        filters: list[str] = []
        for index, (start, segment_seconds) in enumerate(segments):
            inputs.extend(["-ss", str(start), "-t", str(segment_seconds), "-i", str(path)])
            filters.append(
                f"[{index}:a]aformat=sample_rates=16000:channel_layouts=mono,"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        if len(segments) == 1:
            filter_graph = f"{filters[0]};[a0]anull[out]"
        else:
            joined = "".join(f"[a{index}]" for index in range(len(segments)))
            filter_graph = ";".join(filters) + f";{joined}concat=n={len(segments)}:v=0:a=1[out]"
        try:
            completed = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    *inputs,
                    "-filter_complex", filter_graph,
                    "-map", "[out]", "-vn", "-ac", "1", "-ar", "16000", str(preview_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            preview_path.unlink(missing_ok=True)
            return path, None
        if completed.returncode != 0 or not preview_path.exists() or preview_path.stat().st_size == 0:
            preview_path.unlink(missing_ok=True)
            return path, None
        return preview_path, preview_path

    @staticmethod
    def _duration_with_ffprobe(path: Path) -> str:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return format_time(completed.stdout.strip()) if completed.returncode == 0 else ""


def excel_text(value: str) -> str:
    return f'="{value}"'


def boolean_flag(value: str, *, truthy_words: tuple[str, ...], default: str = "0") -> str:
    cleaned = clean_value(value).lower()
    if not cleaned:
        return default
    if cleaned in PLACEHOLDER_VALUES or cleaned.replace(" ", "_").startswith("needs_review"):
        return default
    if cleaned in {"1", "true", "yes", "y"}:
        return "1"
    if cleaned in {"0", "false", "no", "n"}:
        return "0"
    return "1" if any(word in cleaned for word in truthy_words) else "0"


def non_placeholder(value: str, fallback: str) -> str:
    cleaned = clean_value(value)
    return cleaned if cleaned and not is_missing_identity(cleaned) else fallback


def song_key(title: str, artist: str) -> str:
    return f"{canonical_value(artist)}::{canonical_title(title)}"


def output_identity_key(output_row: dict[str, str]) -> tuple[str, str, str, tuple[str, ...]]:
    """Return the portal fields that define an exact duplicate row."""

    tag_keys = tuple(
        sorted(
            {
                canonical_value(value)
                for value in split_tag_values(output_row.get("tag", ""))
                if canonical_value(value)
            }
        )
    )
    return (
        canonical_value(output_row.get("title", "")),
        canonical_value(output_row.get("artist", "")),
        canonical_value(output_row.get("genre", "")),
        tag_keys,
    )


def output_row_quality_score(output_row: dict[str, str]) -> int:
    """Rank duplicate filename candidates without inventing missing facts."""

    score = sum(5 for field in ("album", "tempo", "filename", "year", "label", "tag") if clean_value(output_row.get(field, "")))
    score += 10 if not is_unverified_title(output_row.get("title", "")) else -80
    score += 10 if not is_unverified_artist(output_row.get("artist", "")) else -60
    score += 10 if not is_unverified_genre(output_row.get("genre", "")) else -50
    score += 5 if not is_unverified_language(output_row.get("language", "")) else -20
    if time_to_seconds(output_row.get("time", "")) > 0:
        score += 15
    vocal = output_row.get("vocal", "")
    instrumental = output_row.get("instrumental", "")
    if vocal in {"0", "1"} and instrumental in {"0", "1"} and int(vocal) + int(instrumental) == 1:
        score += 10

    title_key = canonical_value(output_row.get("title", ""))
    artist_key = canonical_value(output_row.get("artist", ""))
    filename_artist, _filename_title = parse_artist_title_from_filename(output_row.get("filename", ""))
    filename_artist_key = canonical_value(filename_artist)
    has_artist_prefix = bool(
        (artist_key and (title_key == artist_key or title_key.startswith(f"{artist_key} ")))
        or (
            filename_artist_key
            and (title_key == filename_artist_key or title_key.startswith(f"{filename_artist_key} "))
        )
    )
    score += -25 if has_artist_prefix else 25
    if artist_key and filename_artist_key and artist_key == filename_artist_key:
        score += 20
    _filename_artist, filename_title = parse_artist_title_from_filename(output_row.get("filename", ""))
    if filename_title and canonical_title(output_row.get("title", "")) == canonical_title(filename_title):
        score += 20
    return score


def cleaning_issues(output_row: dict[str, str]) -> list[str]:
    """Return the issue iii/iv reasons that require removing a converted row."""

    issues: list[str] = []
    if time_to_seconds(output_row.get("time", "")) <= 0:
        issues.append("duration not resolved")

    if is_unverified_title(output_row.get("title", "")):
        issues.append("title invalid")
    if is_unverified_artist(output_row.get("artist", "")):
        issues.append("artist invalid")
    if is_unverified_genre(output_row.get("genre", "")):
        issues.append("genre invalid")
    if is_unverified_language(output_row.get("language", "")):
        issues.append("language invalid")

    for field in ("tempo", "year", "vocal", "instrumental"):
        if not clean_value(output_row.get(field, "")):
            issues.append(f"{field} missing")
    year = output_row.get("year", "")
    if year and (not year.isdigit() or not 1900 <= int(year) <= 2100):
        issues.append("year invalid")
    if clean_value(output_row.get("tempo", "")) and not MediaDurationResolver._numeric_bpm(output_row.get("tempo", "")):
        issues.append("tempo invalid")
    vocal = output_row.get("vocal", "")
    instrumental = output_row.get("instrumental", "")
    if vocal not in {"0", "1"} or instrumental not in {"0", "1"} or int(vocal) + int(instrumental) != 1:
        issues.append("vocal/instrumental invalid")
    return issues


def enforce_distinct_tags(output_row: dict[str, str]) -> None:
    blocked = {
        canonical_value(output_row.get("title", "")),
        canonical_value(output_row.get("artist", "")),
        canonical_value(output_row.get("genre", "")),
    }
    tags: list[str] = []
    seen: set[str] = set()
    for value in split_tag_values(output_row.get("tag", "")):
        key = canonical_value(value)
        if not key or key in blocked or key in seen:
            continue
        seen.add(key)
        tags.append(value)
    if not tags:
        output_row["tag"] = ""
        return
    output_row["tag"] = ",".join(tags)


def enforce_distinct_core_fields(output_row: dict[str, str]) -> None:
    filename = output_row.get("filename", "")
    parsed_artist, parsed_title = parse_artist_title_from_filename(filename)

    title_key = canonical_title(output_row.get("title", ""))
    artist_key = canonical_value(output_row.get("artist", ""))
    genre_key = canonical_value(output_row.get("genre", ""))

    if title_key and artist_key and title_key == artist_key:
        if parsed_artist and canonical_value(parsed_artist) != title_key:
            output_row["artist"] = parsed_artist
        else:
            output_row["artist"] = ""

    title_key = canonical_title(output_row.get("title", ""))
    artist_key = canonical_value(output_row.get("artist", ""))
    if parsed_title and title_key and canonical_title(parsed_title) == title_key and parsed_artist:
        output_row["artist"] = parsed_artist

    genre_key = canonical_value(output_row.get("genre", ""))
    if genre_key and genre_key in {title_key, artist_key}:
        output_row["genre"] = ""


def downloaded_flag(value: str) -> str:
    cleaned = clean_value(value).lower()
    if cleaned in {"1", "true", "yes", "y"}:
        return "1"
    return "0"


def build_output_row(
    row: dict[str, str],
    header_map: dict[str, str],
    duration_resolver: MediaDurationResolver,
    language_resolver: MissingLanguageResolver | None = None,
    *,
    excel_time_text: bool = False,
    csv_context: str = "",
    genre_by_song: dict[str, str] | None = None,
) -> dict[str, str]:
    vocals = row_value(row, header_map, "vocal", "vocals")
    instrumental = row_value(row, header_map, "instrumental")
    time_value = row_value(row, header_map, "time", "duration_seconds", "duration_s", "duration", "length")
    resolved_time = duration_resolver.resolve_time(row, header_map, time_value, csv_context)
    resolved_bpm = duration_resolver.resolve_bpm(
        row,
        header_map,
        row_value(row, header_map, "tempo", "bpm"),
        csv_context,
    )
    vocal_flag, instrumental_flag = duration_resolver.resolve_vocal_flags(
        row,
        header_map,
        vocals,
        instrumental,
        csv_context,
    )
    title = resolve_title(row, header_map, duration_resolver, csv_context)
    artist = resolve_artist(row, header_map, duration_resolver, csv_context)
    genre = resolve_single_genre(row, header_map, resolved_title=title, resolved_artist=artist)
    album = DEFAULT_ALBUM
    existing_language = row_value(row, header_map, "language")
    language = (
        language_resolver.resolve(
            existing_language,
            title=title,
            artist=artist,
            album=album,
            csv_context=csv_context,
        )
        if language_resolver
        else non_placeholder(existing_language, "")
    )
    key = song_key(title, artist)
    if genre_by_song is not None and key != "::":
        if key in genre_by_song:
            genre = genre_by_song[key]
        else:
            genre_by_song[key] = genre

    output_row = {
        "title": non_placeholder(title, fallback_from_filename(filename_for_row(row, header_map), "title")),
        "album": album,
        "artist": non_placeholder(artist, fallback_from_filename(filename_for_row(row, header_map), "artist")),
        "time": excel_text(resolved_time) if excel_time_text else resolved_time,
        "genre": non_placeholder(genre, ""),
        "tempo": resolved_bpm,
        "filename": non_placeholder(row_value(row, header_map, "filename"), basename_from_value(row_value(row, header_map, "file_path", "path"))),
        "year": non_placeholder(row_value(row, header_map, "year"), ""),
        "language": language,
        "isDL": "0",
        "label": DEFAULT_LABEL,
        "vocal": vocal_flag,
        "instrumental": instrumental_flag,
        "tag": build_tag(row, header_map, resolved_title=title, resolved_artist=artist, resolved_genre=genre),
    }
    enforce_distinct_core_fields(output_row)
    enforce_distinct_tags(output_row)
    return output_row


def normalize_output_extension(value: str) -> str:
    cleaned = clean_value(value or DEFAULT_OUTPUT_EXTENSION).lower()
    if not cleaned:
        return DEFAULT_OUTPUT_EXTENSION
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def output_path_for(input_path: Path, suffix: str, output_extension: str = DEFAULT_OUTPUT_EXTENSION) -> Path:
    extension = normalize_output_extension(output_extension)
    return input_path.with_name(f"{input_path.stem}{suffix}{extension}")


def factual_issues(output_row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for field in ("title", "album", "artist", "genre", "filename", "year", "language", "label", "tag"):
        value = output_row.get(field, "")
        if not value or is_missing_identity(value):
            issues.append(f"{field} not verified")
    if is_unverified_title(output_row.get("title", "")):
        issues.append("title not verified")
    if is_unverified_artist(output_row.get("artist", "")):
        issues.append("artist not verified")
    if is_unverified_genre(output_row.get("genre", "")):
        issues.append("genre not verified")
    if is_unverified_language(output_row.get("language", "")):
        issues.append("language not verified")
    for field in ("title", "album", "artist", "filename", "label"):
        if contains_mojibake(output_row.get(field, "")):
            issues.append(f"{field} contains unresolved encoding damage")

    title_key = canonical_title(output_row.get("title", ""))
    artist_key = canonical_value(output_row.get("artist", ""))
    genre_key = canonical_value(output_row.get("genre", ""))
    if title_key and artist_key and title_key == artist_key:
        issues.append("title and artist are not distinct")
    if genre_key and genre_key in {title_key, artist_key}:
        issues.append("genre conflicts with track identity")

    tag_keys = [canonical_value(value) for value in split_tag_values(output_row.get("tag", ""))]
    if len(tag_keys) != len(set(tag_keys)):
        issues.append("tags contain duplicates")
    if any(key in {title_key, artist_key, genre_key} for key in tag_keys):
        issues.append("tags duplicate a core identity field")
    year = output_row.get("year", "")
    if year and (not year.isdigit() or not 1900 <= int(year) <= 2100):
        issues.append("year is invalid")
    if time_to_seconds(output_row.get("time", "")) <= 0:
        issues.append("duration not measured")
    if not MediaDurationResolver._numeric_bpm(output_row.get("tempo", "")):
        issues.append("numeric BPM not measured")
    vocal = output_row.get("vocal", "")
    instrumental = output_row.get("instrumental", "")
    if vocal not in {"0", "1"} or instrumental not in {"0", "1"} or int(vocal) + int(instrumental) != 1:
        issues.append("vocal/instrumental not classified")
    return issues


def should_skip_file(path: Path, suffix: str) -> bool:
    name = path.name.lower()
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
        return True
    return path.stem.lower().endswith(suffix.lower())


def upgrade_csv(
    input_path: Path,
    output_path: Path,
    duration_resolver: MediaDurationResolver,
    *,
    excel_time_text: bool = False,
    strict_facts: bool = False,
    show_progress: bool = False,
    language_resolver: MissingLanguageResolver | None = None,
) -> tuple[int, int]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as source_file:
        reader = csv.DictReader(source_file)
        if not reader.fieldnames:
            raise ValueError(f"{input_path} has no CSV header")

        normalized_headers = {normalize_header(header): header for header in reader.fieldnames}
        source_rows = list(reader)
        csv_context = input_path.stem
        genre_by_song: dict[str, str] = {}
        missing_media_rows = 0
        if duration_resolver.has_media_roots():
            media_rows: list[dict[str, str]] = []
            for row in source_rows:
                if duration_resolver.resolve_media_path(row, normalized_headers, csv_context):
                    media_rows.append(row)
                else:
                    missing_media_rows += 1
            source_rows = media_rows

        grouped_source_rows: dict[str, list[tuple[int, dict[str, str]]]] = {}
        for source_index, row in enumerate(source_rows):
            filename_key = duplicate_filename_key(filename_for_row(row, normalized_headers))
            group_key = filename_key or f"__row_{source_index}"
            grouped_source_rows.setdefault(group_key, []).append((source_index, row))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        seen_output_rows: set[tuple[str, str, str, tuple[str, ...]]] = set()
        rows = 0
        tagged_rows = 0
        unresolved: list[str] = []
        removed: list[str] = []
        duplicate_rows = len(source_rows) - len(grouped_source_rows)
        fsync_every_rows = max(1, int(os.getenv("CSV_FSYNC_EVERY_ROWS", "25")))
        print(f"streaming output: {output_path}")
        with output_path.open("w", newline="", encoding="utf-8-sig") as target_file:
            writer = csv.DictWriter(target_file, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            target_file.flush()
            os.fsync(target_file.fileno())
            with tqdm(
                total=len(source_rows),
                desc=f"Converting {input_path.name}",
                unit="row",
                dynamic_ncols=True,
                mininterval=0.5,
                disable=not show_progress,
            ) as progress:
                for source_group in grouped_source_rows.values():
                    candidates: list[tuple[int, int, dict[str, str]]] = []
                    for source_index, row in source_group:
                        current_name = filename_for_row(row, normalized_headers) or row_value(
                            row, normalized_headers, "title"
                        )
                        if current_name:
                            progress.set_postfix_str(clean_value(current_name)[:45], refresh=False)
                        output_row = build_output_row(
                            row,
                            normalized_headers,
                            duration_resolver,
                            language_resolver,
                            excel_time_text=excel_time_text,
                            csv_context=csv_context,
                            genre_by_song=genre_by_song,
                        )
                        candidates.append((output_row_quality_score(output_row), -source_index, output_row))
                        progress.update(1)

                    if not candidates:
                        continue
                    output_row = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]
                    identity = output_identity_key(output_row)
                    if identity in seen_output_rows:
                        continue
                    seen_output_rows.add(identity)
                    removal_issues = cleaning_issues(output_row) if strict_facts else []
                    if removal_issues:
                        removed.append(
                            f"{output_row.get('filename') or output_row.get('title')}: "
                            f"{', '.join(removal_issues)}"
                        )
                        continue

                    issues = factual_issues(output_row) if strict_facts else []
                    if issues:
                        unresolved.append(
                            f"{output_row.get('filename') or output_row.get('title')}: {', '.join(issues)}"
                        )
                    writer.writerow(output_row)
                    target_file.flush()
                    rows += 1
                    if rows % fsync_every_rows == 0:
                        os.fsync(target_file.fileno())
                    if output_row["tag"]:
                        tagged_rows += 1
            os.fsync(target_file.fileno())

        if duplicate_rows or removed or missing_media_rows:
            print(
                f"cleaning complete: duplicate_filename_rows_removed={duplicate_rows}, "
                f"missing_media_rows_skipped={missing_media_rows}, "
                f"unresolved_or_incomplete_rows_removed={len(removed)}"
            )
        if unresolved:
            preview = "\n".join(f"  - {item}" for item in unresolved[:20])
            remainder = len(unresolved) - min(20, len(unresolved))
            suffix = f"\n  ... and {remainder} more" if remainder else ""
            print(
                f"validation warning: {len(unresolved)} row(s) contain unresolved measured facts; "
                f"all unique rows were retained in {output_path}:\n{preview}{suffix}"
            )

    return rows, tagged_rows


def find_csv_files(path: Path, suffix: str) -> list[Path]:
    if path.is_file():
        return [] if should_skip_file(path, suffix) else [path]
    return sorted(
        candidate
        for candidate in path.iterdir()
        if candidate.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS and not should_skip_file(candidate, suffix)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create portal-ready copied CSV files with only the requested "
            "columns and a tag column from subgenre,mood,moods,weather,season,age_group."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT_DIR),
        help="CSV file or output directory to upgrade. Default: OUTPUT_DIR env or output.",
    )
    parser.add_argument(
        "--suffix",
        default=DEFAULT_SUFFIX,
        help=f"Suffix for copied CSV files. Default: {DEFAULT_SUFFIX}",
    )
    parser.add_argument(
        "--output-extension",
        default=os.getenv("CHANGE_OUTPUT_EXTENSION", DEFAULT_OUTPUT_EXTENSION),
        help=f"Extension for copied output files. Default: {DEFAULT_OUTPUT_EXTENSION}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite copied CSV files if they already exist.",
    )
    parser.add_argument(
        "--media-root",
        action="append",
        default=[],
        help="Mounted folder containing MP3/MP4 files. Can be passed multiple times.",
    )
    parser.add_argument(
        "--excel-time-text",
        action="store_true",
        help='Write time as Excel text formula, e.g. ="00:03:16", so Excel displays leading zero hours.',
    )
    parser.add_argument(
        "--allow-unresolved-facts",
        action="store_true",
        help="Retain rows with unresolved media/duration or missing required metadata.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input path does not exist: {input_path}")
        return 2

    csv_files = find_csv_files(input_path, args.suffix)
    if not csv_files:
        print(f"No CSV files found to upgrade in: {input_path}")
        return 0

    duration_resolver = MediaDurationResolver([Path(root) for root in args.media_root])
    language_resolver = MissingLanguageResolver(
        enabled=os.getenv("MISSING_LANGUAGE_LOOKUP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    processed = 0
    for csv_path in csv_files:
        output_path = output_path_for(csv_path, args.suffix, args.output_extension)
        if output_path.exists() and not args.overwrite:
            print(f"skip existing copy: {output_path}")
            continue
        try:
            rows, tagged_rows = upgrade_csv(
                csv_path,
                output_path,
                duration_resolver,
                excel_time_text=args.excel_time_text,
                strict_facts=not args.allow_unresolved_facts,
                show_progress=True,
                language_resolver=language_resolver,
            )
        except ValueError as exc:
            print(f"failed: {csv_path}\n{exc}")
            return 2
        processed += 1
        print(f"created: {output_path} ({tagged_rows}/{rows} rows tagged)")

    print(f"done. upgraded_files={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
