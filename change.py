from __future__ import annotations

import argparse
import csv
import os
import subprocess
from pathlib import Path

from mutagen import File as MutagenFile


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


class MediaDurationResolver:
    def __init__(self, media_roots: list[Path]) -> None:
        self.media_roots = [root for root in media_roots if root.exists()]
        self._name_index: dict[str, list[Path]] | None = None
        self._relative_index: dict[str, Path] | None = None
        self._duration_cache: dict[Path, str] = {}

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
        if self._name_index is not None and self._relative_index is not None:
            return
        name_index: dict[str, list[Path]] = {}
        relative_index: dict[str, Path] = {}
        for root in self.media_roots:
            for candidate in root.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTENSIONS:
                    name_index.setdefault(candidate.name.casefold(), []).append(candidate)
                    try:
                        relative = candidate.relative_to(root)
                    except ValueError:
                        relative = Path(candidate.name)
                    relative_index.setdefault(self._index_key(relative), candidate)
        self._name_index = name_index
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
    if cleaned in {"1", "true", "yes", "y"}:
        return "1"
    if cleaned in {"0", "false", "no", "n"}:
        return "0"
    return "1" if any(word in cleaned for word in truthy_words) else "0"


def build_output_row(
    row: dict[str, str],
    header_map: dict[str, str],
    duration_resolver: MediaDurationResolver,
    *,
    excel_time_text: bool = False,
    csv_context: str = "",
) -> dict[str, str]:
    vocals = row_value(row, header_map, "vocal", "vocals")
    instrumental = row_value(row, header_map, "instrumental")
    instruments = row_value(row, header_map, "instruments")
    time_value = row_value(row, header_map, "time", "duration_seconds", "duration_s", "duration", "length")
    resolved_time = duration_resolver.resolve_time(row, header_map, time_value, csv_context)
    existing_isdl = row_value(row, header_map, "isDL", "isdl")

    return {
        "title": row_value(row, header_map, "title"),
        "album": row_value(row, header_map, "album"),
        "artist": row_value(row, header_map, "artist"),
        "time": excel_text(resolved_time) if excel_time_text else resolved_time,
        "genre": row_value(row, header_map, "genre"),
        "tempo": row_value(row, header_map, "tempo", "bpm"),
        "filename": row_value(row, header_map, "filename"),
        "year": row_value(row, header_map, "year"),
        "language": row_value(row, header_map, "language"),
        "isDL": duration_resolver.is_downloaded(row, header_map, csv_context)
        if duration_resolver.has_media_roots()
        else existing_isdl or "0",
        "label": row_value(row, header_map, "label", "publisher"),
        "vocal": vocals or boolean_flag(vocals, truthy_words=("vocal", "voice", "sing")),
        "instrumental": instrumental
        or boolean_flag(" ".join((vocals, instruments)), truthy_words=("instrumental", "no vocal", "non vocal")),
        "tag": build_tag(row, header_map),
    }


def output_path_for(input_path: Path, suffix: str) -> Path:
    return input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}")


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
) -> tuple[int, int]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as source_file:
        reader = csv.DictReader(source_file)
        if not reader.fieldnames:
            raise ValueError(f"{input_path} has no CSV header")

        normalized_headers = {normalize_header(header): header for header in reader.fieldnames}
        csv_context = input_path.stem

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8-sig") as target_file:
            writer = csv.DictWriter(target_file, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            rows = 0
            tagged_rows = 0
            for row in reader:
                output_row = build_output_row(
                    row,
                    normalized_headers,
                    duration_resolver,
                    excel_time_text=excel_time_text,
                    csv_context=csv_context,
                )
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
    processed = 0
    for csv_path in csv_files:
        output_path = output_path_for(csv_path, args.suffix)
        if output_path.exists() and not args.overwrite:
            print(f"skip existing copy: {output_path}")
            continue
        rows, tagged_rows = upgrade_csv(
            csv_path,
            output_path,
            duration_resolver,
            excel_time_text=args.excel_time_text,
        )
        processed += 1
        print(f"created: {output_path} ({tagged_rows}/{rows} rows tagged)")

    print(f"done. upgraded_files={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
