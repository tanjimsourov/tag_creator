from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from .config import ConfigError, Settings, load_settings
from .csv_store import CsvStore
from .logging_setup import configure_logging
from .pipeline import enrich_library
from .providers import provider_status
from .rate_limit import RateLimiter
from .resource_limits import apply_process_thread_limits


def _check_config(settings: Settings) -> int:
    store = CsvStore(settings.data_dir)
    try:
        rows = provider_status(settings, store, RateLimiter(settings.rate_limits))
    finally:
        store.close()
    print("Tag Creator - resolved configuration")
    print(f"  input_dir            = {settings.input_dir}  (exists={settings.input_dir.exists()})")
    print(f"  output_dir           = {settings.output_dir}")
    print(f"  data_dir             = {settings.data_dir}")
    print(f"  report_csv           = {settings.report_csv}")
    print(f"  final_csv            = {settings.final_csv}")
    print(f"  worker_threads       = {settings.worker_threads}")
    print(f"  cpu_threads          = {settings.cpu_threads}")
    print(f"  hybrid_mode          = {settings.hybrid_mode}")
    print(f"  min_field_confidence = {settings.min_field_confidence}")
    print(f"  min_write_confidence = {settings.min_write_confidence}")
    print(f"  local_ai_enabled     = {settings.local_ai_enabled}")
    print(f"  max_paid_calls       = {settings.max_paid_calls}")
    print(f"  supported_extensions = {', '.join(settings.supported_extensions)}")
    print("  providers (will_run):")
    ready = [row["provider"] for row in rows if row["will_run"] == "yes"]
    waiting = [row["provider"] for row in rows if row["will_run"] != "yes"]
    print(f"    ready   = {', '.join(ready) or 'none'}")
    print(f"    waiting = {', '.join(waiting) or 'none'}")
    print("Configuration OK.")
    return 0


def _compact(settings: Settings) -> int:
    store = CsvStore(settings.data_dir)
    compacted = store.compact(force=True)
    store.close()
    print(f"Compacted CSV store at {settings.data_dir}: {', '.join(compacted) or 'nothing to compact'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich media tags from multiple open-source metadata providers.")
    parser.add_argument("--input-dir")
    parser.add_argument("--report")
    parser.add_argument("--final-csv", action="store_true", help="write the clean final dataset CSV instead of the debug report")
    parser.add_argument("--no-debug-output", action="store_true", help="do not write JSONL or run_summary side files")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, help="parallel worker threads (default from WORKER_THREADS/CPU count)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--write-cover-art", action="store_true")
    parser.add_argument("--backup-dir", help="copy each original here before writing tags")
    parser.add_argument("--verify-after-write", action="store_true", help="re-read tags after writing to confirm")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--check-config", action="store_true", help="validate settings + provider readiness, then exit")
    parser.add_argument("--compact", action="store_true", help="compact the CSV cache/state files, then exit")
    parser.add_argument("--max-paid-calls", type=int, help="hard cap on paid-provider calls this run")
    parser.add_argument("--log-dir", help="write rotating logs here (default from LOG_DIR)")
    parser.add_argument("--log-json", action="store_true", help="emit JSON-structured logs")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    log_dir = Path(args.log_dir).resolve() if args.log_dir else settings.log_dir
    run_id = configure_logging(args.verbose, log_dir=log_dir, json_format=args.log_json or settings.log_json)

    if args.check_config:
        return _check_config(settings)
    if args.compact:
        return _compact(settings)

    settings = dataclasses.replace(
        settings,
        dry_run=True if args.dry_run else (False if args.write else settings.dry_run),
        write_tags=True if args.write else settings.write_tags,
        write_cover_art=True if args.write_cover_art else settings.write_cover_art,
        resume=False if args.no_resume else settings.resume,
        worker_threads=args.workers if args.workers else settings.worker_threads,
        backup_dir=Path(args.backup_dir).resolve() if args.backup_dir else settings.backup_dir,
        verify_after_write=True if args.verify_after_write else settings.verify_after_write,
        max_paid_calls=args.max_paid_calls if args.max_paid_calls is not None else settings.max_paid_calls,
    )
    apply_process_thread_limits(settings)
    store = CsvStore(settings.data_dir)
    try:
        summary = enrich_library(
            settings,
            store,
            input_dir=Path(args.input_dir).resolve() if args.input_dir else None,
            report_csv=Path(args.report).resolve() if args.report else (settings.final_csv if args.final_csv else None),
            limit=args.limit,
            final_csv=args.final_csv,
            debug_output=not args.no_debug_output,
        )
    except KeyboardInterrupt:
        # Belt-and-suspenders: the pipeline already handles interrupts, but if one
        # escapes, the finally below still flushes state so the run stays resumable.
        print("Interrupted; state flushed. Rerun the same command to resume.", file=sys.stderr)
        return 130
    finally:
        store.close()

    updated = summary.count("updated")
    no_change = summary.count("no_change")
    failed = summary.count("failed") + summary.count("write_failed")
    print(
        f"[{run_id}] Processed {summary.written_rows} files (skipped {summary.skipped}) in "
        f"{summary.duration_seconds}s with {summary.workers} worker(s). "
        f"Updated={updated}, no_change={no_change}, failed={failed}, paid_calls={summary.paid_calls}"
    )
    print(f"{'Final CSV' if args.final_csv else 'Report'}: {summary.report_path}")
    if not args.no_debug_output:
        print(f"Run summary: {settings.output_dir / 'run_summary.json'}")
    return 0
