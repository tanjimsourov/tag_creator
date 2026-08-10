from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


SOURCE_COLUMNS = ("subgenre", "mood", "moods", "weather", "season", "age_group")
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

        fieldnames = list(reader.fieldnames)
        normalized_headers = {normalize_header(header): header for header in fieldnames}
        if "tag" not in normalized_headers:
            fieldnames.append("tag")
        tag_header = normalized_headers.get("tag", "tag")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8-sig") as target_file:
            writer = csv.DictWriter(target_file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            rows = 0
            tagged_rows = 0
            for row in reader:
                tag_value = build_tag(row, normalized_headers)
                row[tag_header] = tag_value
                writer.writerow(row)
                rows += 1
                if tag_value:
                    tagged_rows += 1

    return rows, tagged_rows


def find_csv_files(path: Path, suffix: str) -> list[Path]:
    if path.is_file():
        return [] if should_skip_file(path, suffix) else [path]
    return sorted(candidate for candidate in path.glob("*.csv") if not should_skip_file(candidate, suffix))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create copied CSV files with a new tag column from "
            "subgenre,mood,moods,weather,season,age_group."
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
