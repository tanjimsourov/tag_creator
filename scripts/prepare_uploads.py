#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import PROJECT_ROOT, resolve_path
from tag_creator.upload_manifest import (
    DEFAULT_MEDIA_EXTENSIONS,
    default_input_dirs_from_env,
    parse_excluded_dir_names,
    parse_extensions,
    prepare_uploads,
)


def _env_path(name: str, default: str) -> Path:
    return resolve_path(os.getenv(name, default).strip() or default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect new uploaded media files and safely clean filenames using a JSON manifest."
    )
    parser.add_argument(
        "--input-dir",
        action="append",
        default=[],
        help="Folder to scan. Can be passed multiple times. Defaults to UPLOAD_INPUT_DIRS or INPUT_DIR from .env.",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="Manifest JSON path. Default: UPLOAD_MANIFEST or DATA_DIR/upload_manifest.json.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional prepared output folder. If omitted, clean names are renamed in place.",
    )
    parser.add_argument(
        "--extensions",
        default="",
        help="Comma-separated extensions. Default: SUPPORTED_EXTENSIONS or common audio/video extensions.",
    )
    parser.add_argument(
        "--exclude-dirs",
        default="",
        help="Comma-separated folder names to skip. Default: EXCLUDED_MEDIA_DIR_NAMES or normalized.",
    )
    parser.add_argument("--limit", type=int, help="Optional test limit.")
    parser.add_argument("--mark-existing", action="store_true", help="Record current files as already processed.")
    parser.add_argument(
        "--rename-known",
        action="store_true",
        help="Also rename files already recorded in the manifest, useful after enabling metadata-based names.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually write the manifest and rename/copy files.")
    parser.add_argument("--show-files", action="store_true", help="Print all planned file actions.")
    return parser


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()

    raw_input_dirs = args.input_dir or default_input_dirs_from_env()
    if not raw_input_dirs:
        print("No input folders configured. Pass --input-dir or set UPLOAD_INPUT_DIRS/INPUT_DIR in .env.", file=sys.stderr)
        return 2

    roots = [resolve_path(raw_path) for raw_path in raw_input_dirs]
    missing = [root for root in roots if not root.exists() or not root.is_dir()]
    if missing:
        print("Missing input folder(s):", file=sys.stderr)
        for root in missing:
            print(f"  - {root}", file=sys.stderr)
        return 2

    data_dir_default = os.getenv("DATA_DIR", "data").strip() or "data"
    manifest_path = (
        resolve_path(args.manifest)
        if args.manifest
        else _env_path("UPLOAD_MANIFEST", f"{data_dir_default}/upload_manifest.json")
    )
    output_dir = resolve_path(args.output_dir) if args.output_dir else None
    extensions = parse_extensions(
        args.extensions
        or os.getenv("SUPPORTED_EXTENSIONS", "")
        or ",".join(sorted(DEFAULT_MEDIA_EXTENSIONS))
    )
    excluded = parse_excluded_dir_names(args.exclude_dirs or os.getenv("EXCLUDED_MEDIA_DIR_NAMES", "normalized"))

    summary = prepare_uploads(
        roots,
        manifest_path=manifest_path,
        extensions=extensions,
        excluded_dir_names=excluded,
        output_dir=output_dir,
        apply=args.apply,
        mark_existing=args.mark_existing,
        rename_known=args.rename_known,
        limit=args.limit,
    )

    mode = "apply" if args.apply else "dry-run"
    action = "mark-existing" if args.mark_existing else ("copy" if output_dir else "rename")
    print(
        f"prepare uploads complete: mode={mode}, action={action}, scanned={summary.scanned}, "
        f"known={summary.known}, marked_existing={summary.marked_existing}, planned={summary.planned}, "
        f"renamed={summary.renamed}, copied={summary.copied}, unchanged={summary.unchanged}"
    )
    print(f"manifest: {summary.manifest_path}")

    preview_limit = len(summary.files) if args.show_files else min(20, len(summary.files))
    for item in summary.files[:preview_limit]:
        print(f"  - {item.status}: {item.source_path} -> {item.target_path}")
    if len(summary.files) > preview_limit:
        print(f"  ... and {len(summary.files) - preview_limit} more")
    if not args.apply:
        print("dry-run only. Add --apply to write the manifest and perform file changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
