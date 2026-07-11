#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a CSV of files that still need paid AI analysis after free/web stages."
    )
    parser.add_argument("--report", required=True, help="Enrichment report CSV from dry-run/free-web pass.")
    parser.add_argument("--output", default="output/paid_candidates.csv")
    parser.add_argument(
        "--required-any",
        default="genre,subgenre,mood,bpm,key,language,energy,instruments",
        help="Comma-separated tags that make a file a paid candidate when missing.",
    )
    args = parser.parse_args()

    report_path = Path(args.report)
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    important = {item.strip() for item in args.required_any.split(",") if item.strip()}
    with report_path.open(newline="", encoding="utf-8-sig") as csv_file:
        rows = list(csv.DictReader(csv_file))

    candidates = []
    for row in rows:
        missing = {item.strip() for item in row.get("missing_required", "").split(";") if item.strip()}
        if missing.intersection(important):
            row["paid_candidate_reason"] = "; ".join(sorted(missing.intersection(important)))
            candidates.append(row)

    fieldnames = list(rows[0].keys()) if rows else []
    if "paid_candidate_reason" not in fieldnames:
        fieldnames.append("paid_candidate_reason")
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(candidates)

    print(f"Wrote {len(candidates)} paid candidates to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
