#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import PROJECT_ROOT, load_settings, resolve_path
from tag_creator.csv_store import CsvStore
from tag_creator.logging_setup import configure_logging
from tag_creator.pipeline import enrich_library
from tag_creator.resource_limits import apply_process_thread_limits
from tag_creator.upload_manifest import (
    DEFAULT_MEDIA_EXTENSIONS,
    default_input_dirs_from_env,
    parse_excluded_dir_names,
    parse_extensions,
    prepare_uploads,
)


def _env_path(name: str, default: str) -> Path:
    return resolve_path(os.getenv(name, default).strip() or default)


def _run_date(value: str) -> str:
    if not value:
        return date.today().isoformat()
    normalized = value.strip().replace("/", "-")
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _run_command(args: list[str]) -> None:
    print("running:", " ".join(args))
    completed = subprocess.run(args, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(args)}")


@contextmanager
def pipeline_lock(lock_dir: Path, *, wait_seconds: int) -> Iterator[None]:
    lock_dir = lock_dir.resolve()
    while True:
        try:
            lock_dir.mkdir(parents=True, exist_ok=False)
            (lock_dir / "owner.txt").write_text(
                f"pid={os.getpid()}\nstarted_at={datetime.now().isoformat(timespec='seconds')}\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            print(f"another pipeline run is active, waiting {wait_seconds}s: {lock_dir}")
            time.sleep(max(1, wait_seconds))

    try:
        yield
    finally:
        for path in lock_dir.glob("*"):
            if path.is_file():
                path.unlink()
        lock_dir.rmdir()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run new-file detection, in-place filename cleanup, final CSV conversion, tag CSV, and clean split output."
    )
    parser.add_argument(
        "--input-dir",
        action="append",
        default=[],
        help="Folder to scan. Use once for each media root. Defaults to UPLOAD_INPUT_DIRS or INPUT_DIR from .env.",
    )
    parser.add_argument("--name", default="", help="Output dataset name, e.g. LH MP3. Default: input folder name.")
    parser.add_argument("--output-dir", default="", help="Intermediate CSV root. Default: OUTPUT_DIR or output.")
    parser.add_argument("--clean-dir", default="", help="Final clean root. Default: CLEAN_DIR or clean.")
    parser.add_argument("--data-dir", default="", help="Persistent state/cache root. Default: DATA_DIR or data.")
    parser.add_argument(
        "--manifest",
        default="",
        help="Upload manifest path. Default: UPLOAD_MANIFEST or DATA_DIR/upload_manifest.json.",
    )
    parser.add_argument("--run-date", default="", help="Output date folder, YYYY-MM-DD. Default: today.")
    parser.add_argument("--limit", type=int, help="Optional test limit for newly detected files.")
    parser.add_argument("--workers", type=int, help="Scanner worker threads.")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Loop interval. Default: 24.")
    parser.add_argument("--loop", action="store_true", help="Run forever every interval.")
    parser.add_argument("--lock-dir", default="", help="Lock directory. Default: DATA_DIR/clean_pipeline.lock.")
    parser.add_argument("--lock-wait-seconds", type=int, default=60)
    parser.add_argument("--mark-existing", action="store_true", help="Only mark current files as already seen, then stop.")
    parser.add_argument("--rename-known", action="store_true", help="Also rename already-manifested files.")
    parser.add_argument("--allow-unresolved-facts", action="store_true", help="Pass through to change.py.")
    parser.add_argument("--group-by", choices=("top", "parent"), default="top")
    parser.add_argument("--no-debug-output", action="store_true", help="Do not write JSONL/run summary side files.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def run_once(args: argparse.Namespace) -> None:
    raw_input_dirs = args.input_dir or default_input_dirs_from_env()
    if not raw_input_dirs:
        raise RuntimeError("No input folders configured. Pass --input-dir or set UPLOAD_INPUT_DIRS/INPUT_DIR.")

    input_roots = [resolve_path(raw_path) for raw_path in raw_input_dirs]
    missing = [root for root in input_roots if not root.exists() or not root.is_dir()]
    if missing:
        raise RuntimeError("Missing input folder(s): " + ", ".join(str(root) for root in missing))

    settings = load_settings()
    data_dir = resolve_path(args.data_dir or os.getenv("DATA_DIR", "data").strip() or "data")
    output_root = resolve_path(args.output_dir or os.getenv("OUTPUT_DIR", "output"))
    clean_root = resolve_path(args.clean_dir or os.getenv("CLEAN_DIR", "clean"))
    manifest_path = (
        resolve_path(args.manifest)
        if args.manifest
        else _env_path("UPLOAD_MANIFEST", str(data_dir / "upload_manifest.json"))
    )
    run_date = _run_date(args.run_date or os.getenv("PIPELINE_RUN_DATE", ""))
    input_name = args.name.strip() or os.getenv("PIPELINE_INPUT_NAME", "").strip()
    if not input_name:
        input_name = input_roots[0].name if len(input_roots) == 1 else "media"
    dated_output = output_root / run_date
    dated_clean = clean_root / run_date
    source_csv = dated_output / f"{input_name}.csv"
    tagged_csv = dated_output / f"{input_name}_with_tag.csv"

    extensions = parse_extensions(
        os.getenv("SUPPORTED_EXTENSIONS", "")
        or ",".join(sorted(DEFAULT_MEDIA_EXTENSIONS))
    )
    excluded = parse_excluded_dir_names(os.getenv("EXCLUDED_MEDIA_DIR_NAMES", "normalized"))

    prepared = prepare_uploads(
        input_roots,
        manifest_path=manifest_path,
        extensions=extensions,
        excluded_dir_names=excluded,
        apply=True,
        mark_existing=args.mark_existing,
        rename_known=args.rename_known,
        limit=args.limit,
    )
    print(
        f"prepare uploads complete: scanned={prepared.scanned}, known={prepared.known}, "
        f"marked_existing={prepared.marked_existing}, planned={prepared.planned}, "
        f"renamed={prepared.renamed}, unchanged={prepared.unchanged}"
    )
    print(f"manifest: {prepared.manifest_path}")

    if args.mark_existing:
        print("mark-existing complete; scanner/change/split were not run.")
        return

    selected_paths = [item.target_path for item in prepared.files]
    if not selected_paths:
        print(f"no new media files found; no CSV output created for {run_date}.")
        return

    settings = dataclasses.replace(
        settings,
        input_dir=input_roots[0],
        output_dir=output_root,
        data_dir=data_dir,
        dry_run=True,
        write_tags=False,
        resume=True,
        worker_threads=args.workers if args.workers else settings.worker_threads,
    )
    apply_process_thread_limits(settings)
    configure_logging(args.verbose, log_dir=settings.log_dir, json_format=settings.log_json)

    store = CsvStore(settings.data_dir)
    try:
        summary = enrich_library(
            settings,
            store,
            input_dir=input_roots[0],
            report_csv=source_csv,
            limit=args.limit,
            final_csv=True,
            debug_output=not args.no_debug_output,
            input_paths=selected_paths,
        )
    finally:
        store.close()
    print(f"main CSV created: {summary.report_path} (rows={summary.written_rows}, skipped={summary.skipped})")

    change_command = [
        sys.executable,
        str(PROJECT_ROOT / "change.py"),
        "--input",
        str(source_csv),
        "--media-root",
        str(input_roots[0]),
        "--overwrite",
    ]
    if args.allow_unresolved_facts:
        change_command.append("--allow-unresolved-facts")
    _run_command(change_command)

    split_command = [
        sys.executable,
        str(PROJECT_ROOT / "split.py"),
        "--input",
        str(source_csv),
        "--with-tag",
        str(tagged_csv),
        "--output-dir",
        str(dated_clean),
        "--media-root",
        str(input_roots[0]),
        "--group-by",
        args.group_by,
        "--overwrite",
    ]
    _run_command(split_command)
    print(f"clean output created: {dated_clean / input_name}")


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    data_dir = resolve_path(args.data_dir or os.getenv("DATA_DIR", "data").strip() or "data")
    lock_dir = resolve_path(args.lock_dir) if args.lock_dir else _env_path("PIPELINE_LOCK_DIR", str(data_dir / "clean_pipeline.lock"))

    while True:
        started = time.monotonic()
        with pipeline_lock(lock_dir, wait_seconds=args.lock_wait_seconds):
            run_once(args)

        if not args.loop:
            return 0
        elapsed = time.monotonic() - started
        sleep_seconds = max(0.0, (args.interval_hours * 3600.0) - elapsed)
        if sleep_seconds:
            print(f"next run in {round(sleep_seconds)}s")
            time.sleep(sleep_seconds)
        else:
            print("interval already passed; starting next run immediately")


if __name__ == "__main__":
    raise SystemExit(main())
