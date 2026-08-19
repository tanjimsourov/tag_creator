from __future__ import annotations

import argparse
import csv
import os
import re
import tempfile
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


DEFAULT_INPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
DEFAULT_OUTPUT_DIR = Path("clean")
DEFAULT_SUFFIX = "_with_tag"
DEFAULT_OUTPUT_EXTENSION = ".xls"
SUPPORTED_TABULAR_EXTENSIONS = {".csv", ".xls"}
DEFAULT_MEDIA_PREFIXES = ("/app/input_media", "/app/mp3", "/app/mp4", "/app/media")
UNMATCHED_GROUP = "_unmatched"


def clean_value(value: object) -> str:
    return unicodedata.normalize("NFC", " ".join(str(value or "").strip().split()))


def normalize_header(value: str) -> str:
    return clean_value(value).lower().replace(" ", "_")


def row_value(row: dict[str, str], header_map: dict[str, str], *candidates: str) -> str:
    for candidate in candidates:
        actual_header = header_map.get(normalize_header(candidate))
        if actual_header:
            value = clean_value(row.get(actual_header, ""))
            if value:
                return value
    return ""


def basename_from_value(value: str) -> str:
    cleaned = clean_value(value).replace("\\", "/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def duplicate_filename_key(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", basename_from_value(value)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", cleaned, flags=re.UNICODE).split())


def safe_csv_name(value: str) -> str:
    cleaned = clean_value(value)
    if cleaned == UNMATCHED_GROUP:
        return UNMATCHED_GROUP
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned or UNMATCHED_GROUP


def strip_csv_context_parts(parts: list[str], csv_context: str) -> list[str]:
    if not csv_context or len(parts) <= 1:
        return parts
    if clean_value(parts[0]).casefold() != clean_value(csv_context).casefold():
        return parts
    return parts[1:]


def relative_media_parts(file_path: str, media_prefixes: tuple[str, ...], csv_context: str = "") -> list[str]:
    normalized = clean_value(file_path).replace("\\", "/").strip()
    if not normalized:
        return []

    stripped = normalized.strip("/")
    lowered = f"/{stripped.casefold()}"
    for prefix in media_prefixes:
        prefix_key = f"/{prefix.strip('/').casefold().rstrip('/')}/"
        if lowered.startswith(prefix_key):
            stripped = stripped[len(prefix.strip("/")) + 1 :]
            break

    parts = [part for part in stripped.split("/") if part]
    if parts and "." in parts[-1]:
        parts = parts[:-1]
    return strip_csv_context_parts(parts, csv_context)


def group_name_from_file_path(
    file_path: str,
    media_prefixes: tuple[str, ...],
    group_by: str,
    csv_context: str = "",
) -> str:
    parts = relative_media_parts(file_path, media_prefixes, csv_context)
    if not parts:
        relative_path = relative_media_path(file_path, media_prefixes)
        if csv_context and relative_path and len(relative_path.parts) == 1 and relative_path.suffix:
            return csv_context
        return UNMATCHED_GROUP
    if group_by == "parent":
        return parts[-1]
    return parts[0]


def relative_media_path(file_path: str, media_prefixes: tuple[str, ...]) -> Path | None:
    normalized = clean_value(file_path).replace("\\", "/").strip()
    if not normalized:
        return None

    stripped = normalized.strip("/")
    lowered = f"/{stripped.casefold()}"
    for prefix in media_prefixes:
        prefix_key = f"/{prefix.strip('/').casefold().rstrip('/')}/"
        if lowered.startswith(prefix_key):
            stripped = stripped[len(prefix.strip("/")) + 1 :]
            return Path(*[part for part in stripped.split("/") if part])

    raw_path = Path(normalized)
    if not raw_path.is_absolute() and raw_path.parent != Path("."):
        return raw_path
    return None


def relative_media_path_candidates(
    file_path: str,
    media_prefixes: tuple[str, ...],
    csv_context: str = "",
) -> list[Path]:
    relative_path = relative_media_path(file_path, media_prefixes)
    if not relative_path:
        return []

    candidates = [relative_path]
    stripped_parts = strip_csv_context_parts(list(relative_path.parts), csv_context)
    if stripped_parts != list(relative_path.parts):
        candidates.append(Path(*stripped_parts))
    return candidates


def media_file_exists(
    file_path: str,
    media_prefixes: tuple[str, ...],
    media_roots: tuple[Path, ...],
    csv_context: str = "",
) -> bool:
    if not media_roots:
        return True

    relative_paths = relative_media_path_candidates(file_path, media_prefixes, csv_context)
    if relative_paths:
        return any((root / relative_path).is_file() for relative_path in relative_paths for root in media_roots)

    raw_path = Path(clean_value(file_path))
    if raw_path.is_absolute():
        return raw_path.is_file()

    return any((root / raw_path).is_file() for root in media_roots)


@dataclass(frozen=True)
class CsvPair:
    source_path: Path
    tagged_path: Path


def should_skip_source(path: Path, suffix: str) -> bool:
    return (
        not path.is_file()
        or path.suffix.lower() not in SUPPORTED_TABULAR_EXTENSIONS
        or path.stem.lower().endswith(suffix.lower())
    )


def normalize_output_extension(value: str) -> str:
    cleaned = clean_value(value or DEFAULT_OUTPUT_EXTENSION).lower()
    if not cleaned:
        return DEFAULT_OUTPUT_EXTENSION
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def tagged_path_candidates(source_path: Path, suffix: str) -> list[Path]:
    preferred = source_path.with_name(f"{source_path.stem}{suffix}{DEFAULT_OUTPUT_EXTENSION}")
    legacy = source_path.with_name(f"{source_path.stem}{suffix}{source_path.suffix}")
    csv_legacy = source_path.with_name(f"{source_path.stem}{suffix}.csv")
    candidates: list[Path] = []
    for candidate in (preferred, legacy, csv_legacy):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def tagged_path_for(source_path: Path, suffix: str) -> Path:
    for candidate in tagged_path_candidates(source_path, suffix):
        if candidate.exists():
            return candidate
    return tagged_path_candidates(source_path, suffix)[0]


def find_csv_pairs(input_path: Path, tagged_path: Path | None, suffix: str) -> list[CsvPair]:
    if input_path.is_file():
        if should_skip_source(input_path, suffix):
            return []
        return [CsvPair(input_path, tagged_path or tagged_path_for(input_path, suffix))]

    pairs: list[CsvPair] = []
    for source_path in sorted(input_path.iterdir()):
        if should_skip_source(source_path, suffix):
            continue
        for candidate in tagged_path_candidates(source_path, suffix):
            if candidate.exists():
                pairs.append(CsvPair(source_path, candidate))
                break
    return pairs


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no CSV header")
        return list(reader.fieldnames), list(reader)


def build_group_index(
    source_rows: list[dict[str, str]],
    source_headers: list[str],
    *,
    media_prefixes: tuple[str, ...],
    group_by: str,
    media_roots: tuple[Path, ...] = (),
    csv_context: str = "",
) -> tuple[dict[str, deque[str]], int]:
    header_map = {normalize_header(header): header for header in source_headers}
    groups_by_filename: dict[str, deque[str]] = defaultdict(deque)
    skipped_missing_media = 0

    for row in source_rows:
        file_path = row_value(row, header_map, "file_path", "path")
        if media_roots and not media_file_exists(file_path, media_prefixes, media_roots, csv_context):
            skipped_missing_media += 1
            continue
        filename = row_value(row, header_map, "filename") or basename_from_value(file_path)
        filename_key = duplicate_filename_key(filename)
        if not filename_key:
            continue
        group_name = group_name_from_file_path(file_path, media_prefixes, group_by, csv_context)
        groups_by_filename[filename_key].append(group_name)

    return groups_by_filename, skipped_missing_media


def split_tagged_rows(
    tagged_rows: list[dict[str, str]],
    tagged_headers: list[str],
    group_index: dict[str, deque[str]],
    *,
    include_unmatched: bool = True,
) -> tuple[dict[str, list[dict[str, str]]], int]:
    header_map = {normalize_header(header): header for header in tagged_headers}
    rows_by_group: dict[str, list[dict[str, str]]] = defaultdict(list)
    unmatched_rows = 0

    for row in tagged_rows:
        filename = row_value(row, header_map, "filename")
        filename_key = duplicate_filename_key(filename)
        group_queue = group_index.get(filename_key)
        if group_queue:
            group_name = group_queue[0]
            if len(group_queue) > 1:
                group_queue.rotate(-1)
        else:
            if not include_unmatched:
                unmatched_rows += 1
                continue
            group_name = UNMATCHED_GROUP
            unmatched_rows += 1
        rows_by_group[group_name].append(row)

    return rows_by_group, unmatched_rows


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, str]], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output exists, pass --overwrite to replace: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8-sig",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".part",
    )
    temporary_path = Path(handle.name)
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def clear_existing_split_files(directory: Path) -> int:
    if not directory.exists():
        return 0

    removed = 0
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in SUPPORTED_TABULAR_EXTENSIONS:
            path.unlink()
            removed += 1
    return removed


def split_pair(
    pair: CsvPair,
    *,
    output_root: Path,
    media_prefixes: tuple[str, ...],
    group_by: str,
    media_roots: tuple[Path, ...] = (),
    output_extension: str = DEFAULT_OUTPUT_EXTENSION,
    overwrite: bool = False,
) -> tuple[int, int, int]:
    if not pair.source_path.exists():
        raise FileNotFoundError(f"source CSV does not exist: {pair.source_path}")
    if not pair.tagged_path.exists():
        raise FileNotFoundError(f"with-tag CSV does not exist: {pair.tagged_path}")

    source_headers, source_rows = read_csv(pair.source_path)
    tagged_headers, tagged_rows = read_csv(pair.tagged_path)
    existing_media_roots = tuple(root for root in media_roots if root.exists())
    group_index, skipped_missing_media = build_group_index(
        source_rows,
        source_headers,
        media_prefixes=media_prefixes,
        media_roots=existing_media_roots,
        group_by=group_by,
        csv_context=pair.source_path.stem,
    )
    rows_by_group, unmatched_rows = split_tagged_rows(
        tagged_rows,
        tagged_headers,
        group_index,
        include_unmatched=not existing_media_roots,
    )

    pair_output_dir = output_root / pair.source_path.stem
    removed_existing_files = clear_existing_split_files(pair_output_dir) if overwrite else 0
    output_suffix = normalize_output_extension(output_extension)
    for group_name, rows in sorted(rows_by_group.items(), key=lambda item: safe_csv_name(item[0]).casefold()):
        output_path = pair_output_dir / f"{safe_csv_name(group_name)}{output_suffix}"
        write_csv_atomic(output_path, tagged_headers, rows, overwrite=overwrite)

    written_rows = sum(len(rows) for rows in rows_by_group.values())
    if removed_existing_files:
        print(f"removed stale split files: {removed_existing_files}")
    if skipped_missing_media:
        print(f"skipped source rows with missing media: {skipped_missing_media}")
    return written_rows, len(rows_by_group), unmatched_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Split a *_with_tag.csv into folder-based CSV files using file_path from the original report CSV."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT_DIR),
        help="Original report CSV or directory containing report CSVs. Default: OUTPUT_DIR env or output.",
    )
    parser.add_argument(
        "--with-tag",
        dest="with_tag",
        default="",
        help="Matching *_with_tag.csv. Optional when --input is a single CSV; inferred by suffix.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory where split files will be created. Default: clean.",
    )
    parser.add_argument(
        "--output-extension",
        default=os.getenv("SPLIT_OUTPUT_EXTENSION", DEFAULT_OUTPUT_EXTENSION),
        help=f"Extension for split output files. Default: {DEFAULT_OUTPUT_EXTENSION}",
    )
    parser.add_argument(
        "--suffix",
        default=DEFAULT_SUFFIX,
        help=f"With-tag suffix to infer matching files. Default: {DEFAULT_SUFFIX}",
    )
    parser.add_argument(
        "--media-prefix",
        action="append",
        default=[],
        help="Prefix to strip from file_path before reading folder names. Can be passed multiple times.",
    )
    parser.add_argument(
        "--media-root",
        action="append",
        default=[],
        help="Mounted folder containing media files. When passed, only existing file_path rows are split.",
    )
    parser.add_argument(
        "--group-by",
        choices=("top", "parent"),
        default="top",
        help="Use the top folder under media root or immediate parent folder. Default: top.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite split CSV files if they already exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    tagged_path = Path(args.with_tag) if args.with_tag else None
    output_root = Path(args.output_dir)
    media_prefixes = tuple(args.media_prefix or DEFAULT_MEDIA_PREFIXES)
    media_roots = tuple(Path(root) for root in args.media_root)

    if tagged_path and input_path.is_dir():
        print("--with-tag can only be used when --input is one CSV file")
        return 2

    pairs = find_csv_pairs(input_path, tagged_path, args.suffix)
    if not pairs:
        print(f"No source/with-tag CSV pairs found for: {input_path}")
        return 0

    processed = 0
    for pair in pairs:
        try:
            rows, group_count, unmatched_rows = split_pair(
                pair,
                output_root=output_root,
                media_prefixes=media_prefixes,
                media_roots=media_roots,
                group_by=args.group_by,
                output_extension=args.output_extension,
                overwrite=args.overwrite,
            )
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            print(f"failed: {pair.source_path}\n{exc}")
            return 2

        processed += 1
        print(
            f"created split files: {output_root / pair.source_path.stem} "
            f"(rows={rows}, files={group_count}, unmatched={unmatched_rows})"
        )

    print(f"done. split_jobs={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
