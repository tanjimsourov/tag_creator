#!/usr/bin/env python3
"""Download open-source Essentia audio models into models/local_ai/.

All models are from the public Essentia model zoo (https://essentia.upf.edu/models/).
Each model's labels are extracted from its metadata JSON so the runner can read a
plain .txt label file. Downloads are resumable (existing files are skipped) and
verified against SHA256 when a checksum is known or provided in a CHECKSUMS file.

Usage:
  python scripts/download_models.py --list
  python scripts/download_models.py                       # core models
  python scripts/download_models.py --only genre_head moodtheme_head
  python scripts/download_models.py --dest models/local_ai --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ZOO = "https://essentia.upf.edu/models"

# key -> spec. `local` filenames match tag_creator/config.py defaults so models
# work out of the box. `sha256` is None until pinned; fill CHECKSUMS.txt or this
# dict on your server after the first verified download.
MODELS: dict[str, dict] = {
    "discogs_embeddings": {
        "url": f"{ZOO}/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
        "local": "discogs-effnet-embeddings.pb",
        "sha256": None,
        "core": True,
    },
    "genre_head": {
        "url": f"{ZOO}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
        "local": "genre_discogs400-discogs-effnet.pb",
        "labels_json": f"{ZOO}/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
        "labels_local": "discogs-effnet-labels.txt",
        "sha256": None,
        "core": True,
    },
    "musicnn": {
        "url": f"{ZOO}/autotagging/msd/msd-musicnn-1.pb",
        "local": "mtg_jamendo_musicnn.pb",
        "labels_json": f"{ZOO}/autotagging/msd/msd-musicnn-1.json",
        "labels_local": "mtg_jamendo_labels.txt",
        "sha256": None,
        "core": True,
    },
    "moodtheme_head": {
        "url": f"{ZOO}/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb",
        "local": "mtg_jamendo_moodtheme-discogs-effnet.pb",
        "labels_json": f"{ZOO}/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json",
        "labels_local": "mtg_jamendo_moodtheme-labels.txt",
        "sha256": None,
        "core": True,
    },
    "instrument_head": {
        "url": f"{ZOO}/classification-heads/mtg_jamendo_instrument/mtg_jamendo_instrument-discogs-effnet-1.pb",
        "local": "mtg_jamendo_instrument-discogs-effnet.pb",
        "labels_json": f"{ZOO}/classification-heads/mtg_jamendo_instrument/mtg_jamendo_instrument-discogs-effnet-1.json",
        "labels_local": "mtg_jamendo_instrument-labels.txt",
        "sha256": None,
        "core": True,
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_checksums(dest_dir: Path) -> dict[str, str]:
    """Read optional 'CHECKSUMS.txt' lines of '<sha256>  <filename>'."""
    path = dest_dir / "CHECKSUMS.txt"
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            mapping[parts[1].strip()] = parts[0].strip().lower()
    return mapping


def _download(url: str, dest: Path, force: bool) -> bool:
    if dest.exists() and not force:
        print(f"  skip (exists): {dest.name}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=120) as response:
            if not response.ok:
                print(f"  ERROR {response.status_code}: {url}", file=sys.stderr)
                return False
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    handle.write(chunk)
    except requests.RequestException as exc:
        print(f"  ERROR downloading {url}: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    print(f"  downloaded: {dest.name} ({dest.stat().st_size // 1024} KiB)")
    return True


def _write_labels_from_json(labels_json_url: str, labels_dest: Path, force: bool) -> None:
    if labels_dest.exists() and not force:
        print(f"  skip (exists): {labels_dest.name}")
        return
    try:
        response = requests.get(labels_json_url, timeout=60)
        response.raise_for_status()
        meta = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  WARN could not fetch labels JSON {labels_json_url}: {exc}", file=sys.stderr)
        return
    classes = meta.get("classes") or meta.get("labels") or []
    if not classes:
        print(f"  WARN no 'classes' in metadata for {labels_dest.name}", file=sys.stderr)
        return
    labels_dest.write_text("\n".join(str(item) for item in classes) + "\n", encoding="utf-8")
    print(f"  labels: {labels_dest.name} ({len(classes)} classes)")


def _verify(dest: Path, expected: str | None) -> None:
    if not expected:
        print(f"  WARN no checksum pinned for {dest.name} (skipping verification)")
        return
    actual = _sha256(dest)
    if actual.lower() == expected.lower():
        print(f"  verified sha256: {dest.name}")
    else:
        print(f"  CHECKSUM MISMATCH {dest.name}: expected {expected}, got {actual}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download open-source Essentia audio models.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "models" / "local_ai"))
    parser.add_argument("--only", nargs="*", help="only these model keys (see --list)")
    parser.add_argument("--force", action="store_true", help="re-download even if the file exists")
    parser.add_argument("--list", action="store_true", help="list available model keys and exit")
    args = parser.parse_args()

    if args.list:
        print("Available models:")
        for key, spec in MODELS.items():
            tag = "core" if spec.get("core") else "optional"
            print(f"  {key:20s} [{tag}] -> {spec['local']}")
        return 0

    dest_dir = Path(args.dest).resolve()
    checksums = _load_checksums(dest_dir)
    keys = args.only or [k for k, v in MODELS.items() if v.get("core")]

    failures = 0
    for key in keys:
        spec = MODELS.get(key)
        if not spec:
            print(f"unknown model key: {key}", file=sys.stderr)
            failures += 1
            continue
        print(f"[{key}]")
        model_dest = dest_dir / spec["local"]
        if not _download(spec["url"], model_dest, args.force):
            failures += 1
            continue
        _verify(model_dest, spec.get("sha256") or checksums.get(spec["local"]))
        if spec.get("labels_json"):
            _write_labels_from_json(spec["labels_json"], dest_dir / spec["labels_local"], args.force)

    print(f"\nDone. dest={dest_dir}  failures={failures}")
    if failures:
        print("Some downloads failed; re-run or check URLs at https://essentia.upf.edu/models/", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
