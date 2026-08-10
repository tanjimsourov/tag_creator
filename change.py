from __future__ import annotations

import argparse
import csv
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
MEDIA_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".aac", ".flac", ".wav", ".wma", ".ogg"}
DEFAULT_ALBUM = "Single"
DEFAULT_LABEL = "SMC"

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


def normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def clean_value(value: str) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    markers = ("\u00c3", "\u00c2", "\u00e2", "\u00f0")
    if any(marker in cleaned for marker in markers):
        for encoding in ("cp1252", "latin-1"):
            try:
                repaired = cleaned.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if sum(repaired.count(marker) for marker in markers) < sum(cleaned.count(marker) for marker in markers):
                cleaned = repaired
                break
    return cleaned


def normalized_filename_key(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", clean_value(value)).casefold()
    stem = Path(cleaned).stem
    suffix = Path(cleaned).suffix
    stem = "".join(character for character in unicodedata.normalize("NFKD", stem) if not unicodedata.combining(character))
    stem = re.sub(r"[^\w]+", "", stem, flags=re.UNICODE)
    return f"{stem}{suffix}"


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
    return lowered not in PLACEHOLDER_VALUES and not normalized.startswith("needs_review")


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


def filename_for_row(row: dict[str, str], header_map: dict[str, str]) -> str:
    return row_value(row, header_map, "filename") or basename_from_value(row_value(row, header_map, "file_path", "path"))


def fallback_from_filename(filename: str, field: str) -> str:
    stem = standardize_lyrics_marker(strip_media_extension(filename))
    artist, title = parse_artist_title_from_filename(filename)
    if field == "artist":
        return artist or stem or DEFAULT_LABEL
    return title or stem or "Untitled"


def resolve_single_genre(row: dict[str, str], header_map: dict[str, str]) -> str:
    raw_genre = row_value(row, header_map, "genre")
    candidates = split_tag_values(raw_genre)
    primary = candidates[0] if candidates else raw_genre
    normalized = normalize_genre_name(primary)
    normalized_parts = split_tag_values(normalized)
    return normalized_parts[0] if normalized_parts else title_case_value(primary)


def normalize_tag_value(value: str, source_column: str) -> str:
    # Only genre-like values should be mapped against the portal genre API.
    # Mood, weather, season and audience tags are separate taxonomies.
    if source_column == "subgenre":
        normalized = normalize_genre_name(value)
        return normalized or title_case_value(value)
    return title_case_value(value)


def resolve_artist(row: dict[str, str], header_map: dict[str, str]) -> str:
    artist = row_value(row, header_map, "artist")
    filename = filename_for_row(row, header_map)
    parsed_artist, parsed_title = parse_artist_title_from_filename(filename)
    title = standardize_lyrics_marker(row_value(row, header_map, "title"))
    if parsed_artist and (
        is_missing_identity(artist)
        or is_missing_identity(title)
        or (parsed_title and title and canonical_title(parsed_title) == canonical_title(title))
    ):
        return parsed_artist
    if not is_missing_identity(artist):
        return artist
    return fallback_from_filename(filename, "artist")


def resolve_title(row: dict[str, str], header_map: dict[str, str]) -> str:
    filename = filename_for_row(row, header_map)
    title = standardize_lyrics_marker(row_value(row, header_map, "title"))
    _, parsed_title = parse_artist_title_from_filename(filename)
    if parsed_title and (
        is_missing_identity(title)
        or has_lyrics_marker(filename, parsed_title)
        or canonical_value(parsed_title).replace(" lyrics", "") == canonical_value(title)
    ):
        return parsed_title
    if not is_missing_identity(title):
        return f"{title} (Lyrics)" if has_lyrics_marker(filename) and "(lyrics)" not in title.lower() else title
    return fallback_from_filename(filename, "title")


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
        if cleaned and not is_missing_identity(cleaned):
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
    def __init__(self, media_roots: list[Path]) -> None:
        self.media_roots = [root for root in media_roots if root.exists()]
        self._name_index: dict[str, list[Path]] | None = None
        self._normalized_name_index: dict[str, list[Path]] | None = None
        self._relative_index: dict[str, Path] | None = None
        self._duration_cache: dict[Path, str] = {}
        self._audio_fact_cache: dict[Path, dict[str, str]] = {}

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

    def resolve_media_path(self, row: dict[str, str], header_map: dict[str, str], csv_context: str = "") -> Path | None:
        raw_file_path = row_value(row, header_map, "file_path", "path")
        filename = row_value(row, header_map, "filename")
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

    def _relative_candidates(self, raw_file_path: str, filename: str, csv_context: str) -> list[Path]:
        candidates: list[Path] = []
        if raw_file_path:
            stripped = self._strip_app_mount(raw_file_path)
            if stripped:
                candidates.append(stripped)
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
        # This converter must preserve a completed source CSV. Provenance such
        # as ``final_completion`` is useful for auditing, but it does not make
        # an explicit vocal/instrumental value missing. Only invoke the slower
        # local model when the existing columns cannot provide a valid pair.
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

        media_path = self.resolve_media_path(row, header_map, csv_context)
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

    def _audio_facts(self, path: Path) -> dict[str, str]:
        cached = self._audio_fact_cache.get(path)
        if cached is not None:
            return cached

        facts: dict[str, str] = {}
        errors: list[str] = []
        analysis_path, temporary_path = self._analysis_preview(path)
        try:
            try:
                from tag_creator.local_ai_runner import run_clap_zero_shot, run_essentia_features
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
                    if ranked:
                        is_instrumental = clean_value(str(ranked[0].get("label", ""))).lower() == "instrumental"
                        facts["vocal"] = "0" if is_instrumental else "1"
                        facts["instrumental"] = "1" if is_instrumental else "0"
                except Exception as exc:  # noqa: BLE001 - preserve independently measured BPM
                    errors.append(f"vocals {type(exc).__name__}: {exc}")
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

        if errors:
            facts["analysis_error"] = "; ".join(errors)

        self._audio_fact_cache[path] = facts
        return facts

    def _analysis_preview(self, path: Path) -> tuple[Path, Path | None]:
        duration = self._duration_for_file(path)
        total_seconds = time_to_seconds(duration)
        preview_seconds = min(60, max(30, total_seconds)) if total_seconds else 45
        start = max(0, (total_seconds - preview_seconds) // 2) if total_seconds else 0
        handle = tempfile.NamedTemporaryFile(prefix="tag_creator_final_", suffix=".wav", delete=False)
        preview_path = Path(handle.name)
        handle.close()
        try:
            completed = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(start), "-i", str(path), "-t", str(preview_seconds),
                    "-vn", "-ac", "1", "-ar", "48000", str(preview_path),
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
            output_row["artist"] = DEFAULT_LABEL

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
    existing_isdl = row_value(row, header_map, "isDL", "isdl")
    title = resolve_title(row, header_map)
    artist = resolve_artist(row, header_map)
    genre = resolve_single_genre(row, header_map)
    album = non_placeholder(row_value(row, header_map, "album"), DEFAULT_ALBUM)
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
        "isDL": duration_resolver.is_downloaded(row, header_map, csv_context)
        if duration_resolver.has_media_roots()
        else downloaded_flag(existing_isdl),
        "label": non_placeholder(row_value(row, header_map, "label", "publisher"), DEFAULT_LABEL),
        "vocal": vocal_flag,
        "instrumental": instrumental_flag,
        "tag": build_tag(row, header_map, resolved_title=title, resolved_artist=artist, resolved_genre=genre),
    }
    enforce_distinct_core_fields(output_row)
    enforce_distinct_tags(output_row)
    return output_row


def output_path_for(input_path: Path, suffix: str) -> Path:
    return input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}")


def factual_issues(output_row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for field in ("title", "artist", "genre", "year", "language", "tag"):
        value = output_row.get(field, "")
        if not value or is_missing_identity(value):
            issues.append(f"{field} not verified")
    year = output_row.get("year", "")
    if year and (not year.isdigit() or not 1900 <= int(year) <= 2100):
        issues.append("year is invalid")
    if output_row.get("isDL") != "1":
        issues.append("media file not resolved")
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
    if not path.is_file() or path.suffix.lower() != ".csv":
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

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(f".{output_path.name}.part")
        seen_output_rows: set[tuple[str, str, str, tuple[str, ...]]] = set()
        rows = 0
        tagged_rows = 0
        unresolved: list[str] = []
        try:
            with temporary_path.open("w", newline="", encoding="utf-8-sig") as target_file:
                writer = csv.DictWriter(target_file, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                with tqdm(
                    source_rows,
                    total=len(source_rows),
                    desc=f"Converting {input_path.name}",
                    unit="row",
                    dynamic_ncols=True,
                    mininterval=0.5,
                    disable=not show_progress,
                ) as progress:
                    for row in progress:
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
                        identity = output_identity_key(output_row)
                        if identity in seen_output_rows:
                            continue
                        seen_output_rows.add(identity)
                        issues = factual_issues(output_row) if strict_facts else []
                        if issues:
                            unresolved.append(
                                f"{output_row.get('filename') or output_row.get('title')}: {', '.join(issues)}"
                            )
                        writer.writerow(output_row)
                        rows += 1
                        if output_row["tag"]:
                            tagged_rows += 1
                target_file.flush()
                os.fsync(target_file.fileno())
            if unresolved:
                preview = "\n".join(f"  - {item}" for item in unresolved[:20])
                remainder = len(unresolved) - min(20, len(unresolved))
                suffix = f"\n  ... and {remainder} more" if remainder else ""
                raise ValueError(
                    f"final CSV rejected: {len(unresolved)} row(s) still contain unresolved measured facts:\n"
                    f"{preview}{suffix}"
                )
            temporary_path.replace(output_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    return rows, tagged_rows


def find_csv_files(path: Path, suffix: str) -> list[Path]:
    if path.is_file():
        return [] if should_skip_file(path, suffix) else [path]
    return sorted(candidate for candidate in path.glob("*.csv") if not should_skip_file(candidate, suffix))


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
        help="Create the copy even when duration, BPM, download state, or vocal classification cannot be verified.",
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
        output_path = output_path_for(csv_path, args.suffix)
        if output_path.exists() and not args.overwrite:
            print(f"skip existing copy: {output_path}")
            continue
        try:
            rows, tagged_rows = upgrade_csv(
                csv_path,
                output_path,
                duration_resolver,
                excel_time_text=args.excel_time_text,
                strict_facts=bool(args.media_root) and not args.allow_unresolved_facts,
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
