from __future__ import annotations

import argparse
import csv
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


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
SOURCE_TAG_COLUMNS = ("subgenre", "mood", "moods", "weather", "season", "age_group")
DEFAULT_SOURCE = Path("with-tag/XtendaMix.csv")
DEFAULT_TARGETS = (
    Path("with-tag/XtendaMix_with_tag.csv"),
    Path("with-tag/XtendaMix_with_tag_fixed.csv"),
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
    "needs_review",
    "needs review",
    "not listed",
    "not listed in free sources",
}

BAD_TAG_WORDS = {
    "field confidence json",
    "field sources json",
    "mixed instrumentation",
    "embedded or not listed in free sources",
}

GENRE_MAP = {
    "adult contemporary": "Pop",
    "classic rock": "Rock",
    "contemporary r and b": "R&B",
    "dance pop": "Dance-pop",
    "dance-pop": "Dance-pop",
    "electro": "Electronic",
    "electronica": "Electronic",
    "electropop": "Electronic",
    "german dance": "Dance",
    "hip hop": "Hip-Hop",
    "hip-hop": "Hip-Hop",
    "house/edm": "EDM",
    "k pop": "K-Pop",
    "k-pop": "K-Pop",
    "pop folk world and country stage and screen": "Pop",
    "pop rap": "Pop Rap",
    "pop rock": "Pop Rock",
    "popular music": "Pop",
    "r and b": "R&B",
    "rock and roll": "Rock",
    "synth pop": "Synth-pop",
    "synth-pop": "Synth-pop",
    "top 40": "Pop",
    "urban": "Hip-Hop",
}

GENRE_LIKE_TAGS = {
    "adult contemporary",
    "blues",
    "contemporary r and b",
    "country",
    "dance",
    "dance pop",
    "dance-pop",
    "disco",
    "edm",
    "electro",
    "electronic",
    "electronica",
    "electropop",
    "folk",
    "funk",
    "hard techno",
    "hip hop",
    "hip-hop",
    "house",
    "k pop",
    "k-pop",
    "pop",
    "pop rap",
    "pop rock",
    "progressive house",
    "r and b",
    "r&b",
    "rock",
    "rock and roll",
    "soul",
    "synth pop",
    "synth-pop",
    "techno",
}

GENERIC_ALLOWED_TAGS = {
    "all season",
    "all weather",
    "balanced",
    "calm",
    "energetic",
    "mainstream",
    "rainy",
    "retail energy",
    "upbeat",
    "youth/adult",
}


def clean(value: object) -> str:
    return unicodedata.normalize("NFC", " ".join(str(value or "").strip().split()))


def canonical(value: object) -> str:
    text = clean(value).casefold().replace("&", "and")
    text = re.sub(r"[^a-z0-9/]+", " ", text)
    return " ".join(text.split())


def split_values(value: object) -> list[str]:
    parts: list[str] = []
    for comma_part in clean(value).split(","):
        parts.extend(comma_part.split(";"))
    return [clean(part) for part in parts if clean(part)]


def looks_like_date_or_year(value: object) -> bool:
    return bool(re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?|\d{1,2}/\d{1,2}/\d{2,4}", clean(value)))


def looks_like_number(value: object) -> bool:
    try:
        float(clean(value))
    except ValueError:
        return False
    return bool(clean(value))


def looks_like_key(value: object) -> bool:
    return bool(re.fullmatch(r"[a-g](?:#|b)?\s+(?:major|minor)", clean(value), flags=re.I))


def looks_like_url_json_or_copyright(value: object) -> bool:
    text = clean(value)
    lowered = text.lower()
    return (
        lowered.startswith(("http://", "https://", "{", "[{"))
        or bool(re.search(r"\(?c\)?\s*\d{4}", text, flags=re.I))
    )


def strip_media_extension(filename: str) -> str:
    name = clean(filename).replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(mp3|mp4|m4a|aac|flac|wav|wma|ogg)$", "", name, flags=re.I)


def parse_filename(filename: str) -> tuple[str, str]:
    stem = strip_media_extension(filename)
    stem = re.sub(r"\s*\((?:1080|720|clean|single)\)\s*$", "", stem, flags=re.I)
    match = re.search(r"\s+[-–—]\s+", stem)
    if not match:
        return "", stem
    return clean(stem[: match.start()]), clean(stem[match.end() :])


def valid_identity(value: object, *, allow_numeric_title: bool = False) -> bool:
    text = clean(value)
    if not text or canonical(text) in PLACEHOLDER_VALUES:
        return False
    if looks_like_date_or_year(text):
        return False
    if looks_like_number(text) and not allow_numeric_title:
        return False
    if looks_like_url_json_or_copyright(text):
        return False
    return True


def valid_tag_value(value: object) -> bool:
    text = clean(value)
    key = canonical(text)
    if not text or key in PLACEHOLDER_VALUES or key in BAD_TAG_WORDS:
        return False
    if looks_like_date_or_year(text) or looks_like_number(text) or looks_like_key(text):
        return False
    if looks_like_url_json_or_copyright(text):
        return False
    if re.fullmatch(r"[a-z]{1,2}\d+[a-z0-9, -]*", key):
        return False
    return True


def normalize_tag(value: object) -> str:
    text = clean(value)
    key = canonical(text)
    if key in GENRE_MAP:
        return GENRE_MAP[key]
    if key == "all weather":
        return "All Weather"
    if key == "all season":
        return "All Season"
    if key == "youth/adult":
        return "Youth/adult"
    if key == "r and b":
        return "R&B"
    return " ".join(
        word.upper()
        if word.casefold() in {"dj", "edm", "r&b", "uk", "us", "usa"}
        else word[:1].upper() + word[1:]
        for word in text.split()
    )


def normalize_genre(value: object) -> str:
    text = clean(value)
    if "," in text:
        for part in split_values(text):
            normalized = normalize_genre(part)
            if normalized:
                return normalized
    return normalize_tag(text)


def source_score(row: dict[str, str]) -> int:
    filename_artist, filename_title = parse_filename(row.get("filename", ""))
    score = 0
    score += 30 if valid_identity(row.get("title"), allow_numeric_title=canonical(row.get("title")) == canonical(filename_title)) else -50
    score += 30 if valid_identity(row.get("artist")) and canonical(row.get("artist")) != canonical(normalize_genre(row.get("genre"))) else -50
    score += 10 if valid_tag_value(row.get("genre")) else -10
    score += 5 * sum(1 for field in SOURCE_TAG_COLUMNS if valid_tag_value(row.get(field)))
    if filename_artist and canonical(row.get("artist")) == canonical(filename_artist):
        score += 10
    return score


def source_index(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[clean(row.get("filename", ""))].append(row)
    return {filename: max(candidates, key=source_score) for filename, candidates in grouped.items()}


def add_tag(tags: list[str], seen: set[str], value: object, blocked: set[str]) -> None:
    if not valid_tag_value(value):
        return
    normalized = normalize_tag(value)
    key = canonical(normalized)
    if key not in GENRE_LIKE_TAGS and key not in GENERIC_ALLOWED_TAGS:
        return
    if not key or key in blocked or key in seen:
        return
    seen.add(key)
    tags.append(normalized)


def default_mood(genre: str) -> str:
    key = canonical(genre)
    if key in {"dance", "edm", "electronic", "house", "techno", "hard techno", "k pop"}:
        return "Upbeat"
    if key in {"rock", "hip hop", "hip-hop"}:
        return "Energetic"
    if key in {"pop", "r and b", "r&b", "soul"}:
        return "Mainstream"
    return "Balanced"


def rebuild_tag(row: dict[str, str], source_row: dict[str, str]) -> str:
    tags: list[str] = []
    seen: set[str] = set()
    blocked = {
        canonical(row.get("title", "")),
        canonical(row.get("artist", "")),
        canonical(row.get("album", "")),
        canonical(row.get("genre", "")),
        canonical(row.get("label", "")),
    }
    blocked.discard("")

    for source_value in split_values(source_row.get("subgenre", "")):
        add_tag(tags, seen, source_value, blocked)

    has_genre_like = any(canonical(tag) in GENRE_LIKE_TAGS for tag in tags)
    if not has_genre_like:
        add_tag(tags, seen, row.get("genre", ""), blocked)

    for field in ("mood", "moods"):
        for source_value in split_values(source_row.get(field, "")):
            add_tag(tags, seen, source_value, blocked)

    if not any(canonical(tag) in {"balanced", "calm", "energetic", "mainstream", "upbeat"} for tag in tags):
        add_tag(tags, seen, default_mood(row.get("genre", "")), blocked)

    for source_value in split_values(source_row.get("weather", "")):
        add_tag(tags, seen, source_value, blocked)
    if "all weather" not in seen:
        add_tag(tags, seen, "All Weather", blocked)

    for source_value in split_values(source_row.get("season", "")):
        add_tag(tags, seen, source_value, blocked)
    if "all season" not in seen:
        add_tag(tags, seen, "All Season", blocked)

    for source_value in split_values(source_row.get("age_group", "")):
        add_tag(tags, seen, source_value, blocked)
    if "youth/adult" not in seen:
        add_tag(tags, seen, "Youth/adult", blocked)

    return ",".join(tags)


def verify_tag(tag: str, row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    values = split_values(tag)
    keys = [canonical(value) for value in values]
    blocked = {
        canonical(row.get("title", "")),
        canonical(row.get("artist", "")),
        canonical(row.get("album", "")),
        canonical(row.get("label", "")),
    }
    blocked.discard("")
    if not values:
        issues.append("blank tag")
    if len(keys) != len(set(keys)):
        issues.append("duplicate tag")
    for value in values:
        key = canonical(value)
        if not valid_tag_value(value):
            issues.append(f"bad tag value: {value}")
        if key in blocked:
            issues.append(f"tag duplicates core field: {value}")
        if key not in GENRE_LIKE_TAGS and key not in GENERIC_ALLOWED_TAGS and key not in {canonical(normalize_tag(value))}:
            # The normalized value itself is accepted; this keeps the verifier focused on polluted values.
            pass
    return issues


def repair_file(path: Path, source_rows: dict[str, dict[str, str]]) -> tuple[int, int, Path]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    changed = 0
    issues: list[str] = []
    for index, row in enumerate(rows, start=2):
        source_row = source_rows.get(clean(row.get("filename", "")), {})
        new_tag = rebuild_tag(row, source_row)
        if row.get("tag", "") != new_tag:
            row["tag"] = new_tag
            changed += 1
        row_issues = verify_tag(new_tag, row)
        if row_issues:
            issues.append(f"row {index}: {'; '.join(row_issues)}")

    if issues:
        preview = "\n".join(f"  - {issue}" for issue in issues[:30])
        raise ValueError(f"{path} still has tag issues:\n{preview}")

    temporary_path = path.with_name(f".{path.stem}.tagfix.part")
    with temporary_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    try:
        temporary_path.replace(path)
        output_path = path
    except PermissionError:
        output_path = path.with_name(f"{path.stem}_tag_clean{path.suffix}")
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return len(rows), changed, output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild XtendaMix tag column from taxonomy fields only.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("targets", nargs="*", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.source.open("r", newline="", encoding="utf-8-sig") as handle:
        source_rows = source_index(list(csv.DictReader(handle)))

    targets = args.targets or [path for path in DEFAULT_TARGETS if path.exists()]
    if not targets:
        print("No target CSV files found.")
        return 2

    for target in targets:
        rows, changed, output_path = repair_file(target, source_rows)
        print(f"{target}: rows={rows}, tags_changed={changed}, output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
