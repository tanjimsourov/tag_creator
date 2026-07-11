#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings
from tag_creator.csv_store import CsvStore
from tag_creator.logging_setup import configure_logging
from tag_creator.pipeline import scan_library
from tag_creator.reports import export_existing_tags


def main() -> int:
    parser = argparse.ArgumentParser(description="Export tags currently saved inside media files.")
    parser.add_argument("--input-dir")
    parser.add_argument("--output", default="output/existing_tags.csv")
    parser.add_argument("--preset", choices=["basic", "full"], default="full")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    configure_logging(args.verbose)
    settings = load_settings()
    store = CsvStore(settings.data_dir)
    media_files = scan_library(
        settings,
        store,
        input_dir=Path(args.input_dir).resolve() if args.input_dir else None,
        limit=args.limit,
    )
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parents[1] / output_path
    export_existing_tags(output_path, media_files, args.preset)
    print(f"Exported {len(media_files)} rows to {output_path}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
