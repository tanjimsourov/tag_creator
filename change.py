from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


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
    "not listed in free sources",
}


def normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def clean_value(value: str) -> str:
    return " ".join(str(value or "").strip().split())


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
    return lowered not in PLACEHOLDER_VALUES and not lowered.startswith("needs_review")


def build_tag(row: dict[str, str], header_map: dict[str, str]) -> str:
    tags: list[str] = []
    seen: set[str] = set()

    for source_column in SOURCE_COLUMNS:
        actual_header = header_map.get(source_column)
        if not actual_header:
            continue
        for value in split_tag_values(row.get(actual_header, "")):
            if not is_usable_tag(value):
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            tags.append(value)

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


def boolean_flag(value: str, *, truthy_words: tuple[str, ...], default: str = "0") -> str:
    cleaned = clean_value(value).lower()
    if not cleaned:
        return default
    if cleaned in {"1", "true", "yes", "y"}:
        return "1"
    if cleaned in {"0", "false", "no", "n"}:
        return "0"
    return "1" if any(word in cleaned for word in truthy_words) else "0"


def build_output_row(row: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    vocals = row_value(row, header_map, "vocal", "vocals")
    instruments = row_value(row, header_map, "instrumental", "instruments")
    time_value = row_value(row, header_map, "time", "duration_seconds", "duration_s", "duration", "length")

    return {
        "title": row_value(row, header_map, "title"),
        "album": row_value(row, header_map, "album"),
        "artist": row_value(row, header_map, "artist"),
        "time": format_time(time_value),
        "genre": row_value(row, header_map, "genre"),
        "tempo": row_value(row, header_map, "tempo", "bpm"),
        "filename": row_value(row, header_map, "filename"),
        "year": row_value(row, header_map, "year"),
        "language": row_value(row, header_map, "language"),
        "isDL": row_value(row, header_map, "isDL", "isdl") or "0",
        "label": row_value(row, header_map, "label", "publisher"),
        "vocal": boolean_flag(vocals, truthy_words=("vocal", "voice", "sing")),
        "instrumental": boolean_flag(
            " ".join((vocals, instruments)),
            truthy_words=("instrumental", "no vocal", "non vocal"),
        ),
        "tag": build_tag(row, header_map),
    }


def output_path_for(input_path: Path, suffix: str) -> Path:
    return input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}")


def should_skip_file(path: Path, suffix: str) -> bool:
    name = path.name.lower()
    if not path.is_file() or path.suffix.lower() != ".csv":
        return True
    return path.stem.lower().endswith(suffix.lower())


def upgrade_csv(input_path: Path, output_path: Path) -> tuple[int, int]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as source_file:
        reader = csv.DictReader(source_file)
        if not reader.fieldnames:
            raise ValueError(f"{input_path} has no CSV header")

        normalized_headers = {normalize_header(header): header for header in reader.fieldnames}

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8-sig") as target_file:
            writer = csv.DictWriter(target_file, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            rows = 0
            tagged_rows = 0
            for row in reader:
                output_row = build_output_row(row, normalized_headers)
                writer.writerow(output_row)
                rows += 1
                if output_row["tag"]:
                    tagged_rows += 1

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

    processed = 0
    for csv_path in csv_files:
        output_path = output_path_for(csv_path, args.suffix)
        if output_path.exists() and not args.overwrite:
            print(f"skip existing copy: {output_path}")
            continue
        rows, tagged_rows = upgrade_csv(csv_path, output_path)
        processed += 1
        print(f"created: {output_path} ({tagged_rows}/{rows} rows tagged)")

    print(f"done. upgraded_files={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
