from __future__ import annotations

import csv
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import EnrichmentResult, MediaFile

LOGGER = logging.getLogger(__name__)

# CSV cells can hold very large provider payloads; lift the field-size limit so a
# huge cached JSON response never aborts a read of the store.
try:
    csv.field_size_limit(2**27)
except OverflowError:  # pragma: no cover - platform dependent ceiling
    csv.field_size_limit(2**24)


class CsvStore:
    """CSV-only cache, inventory, and resume state.

    Design goals (production, thousands of files, NO SQLite):

    * **Append-only hot path.** ``set_cache``/``upsert_media``/``save_enrichment``
      buffer a single new row and periodically *append* to the CSV. The whole file
      is never rewritten during a run, so writes stay O(1) and memory stays bounded
      by the number of distinct keys. This removes the previous O(n^2) behaviour
      where every flush rewrote the entire file.
    * **Last-wins index.** On startup each file is streamed once into an in-memory
      index keyed by ``(provider, cache_key)`` / ``path``; duplicate rows are
      tolerated and the latest row wins.
    * **Thread-safe.** All mutating operations take a re-entrant lock so a
      multi-threaded pipeline can share one store safely.
    * **Compaction.** ``compact()`` rewrites a de-duplicated file atomically
      (temp + ``os.replace``). It runs only on ``close()`` (when the file has
      bloated past a ratio) or when explicitly requested via ``--compact`` — never
      on the hot path.

    The files stay human/spreadsheet friendly. No database engine is used.
    """

    API_CACHE_FIELDS = ["provider", "cache_key", "status_code", "response_json", "created_at"]
    MEDIA_FIELDS = [
        "path",
        "size_bytes",
        "mtime",
        "extension",
        "duration_seconds",
        "bitrate",
        "tags_json",
        "has_cover_art",
        "scanned_at",
    ]
    ENRICHMENT_FIELDS = [
        "path",
        "size_bytes",
        "mtime",
        "status",
        "written_fields_json",
        "merged_json",
        "providers_json",
        "error",
        "processed_at",
    ]

    RESUMABLE_STATUSES = {"dry_run_done", "updated", "no_change"}

    def __init__(
        self,
        data_dir: Path,
        flush_every: int = 200,
        compact_on_close: bool = True,
        compact_bloat_ratio: float = 1.5,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.flush_every = max(1, int(flush_every))
        self.compact_on_close = compact_on_close
        self.compact_bloat_ratio = max(1.0, float(compact_bloat_ratio))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.api_cache_path = self.data_dir / "api_cache.csv"
        self.media_inventory_path = self.data_dir / "media_inventory.csv"
        self.enrichment_state_path = self.data_dir / "enrichment_state.csv"

        # name -> (path, fieldnames, key_fn)
        self._specs: dict[str, tuple[Path, list[str], Callable[[dict[str, str]], Any]]] = {
            "api_cache": (self.api_cache_path, self.API_CACHE_FIELDS, self._api_key),
            "media": (self.media_inventory_path, self.MEDIA_FIELDS, self._path_key),
            "enrichment": (self.enrichment_state_path, self.ENRICHMENT_FIELDS, self._path_key),
        }

        self._lock = threading.RLock()
        for path, fields, _ in self._specs.values():
            self._ensure_file(path, fields)

        self._indexes: dict[str, dict[Any, dict[str, str]]] = {}
        self._disk_rows: dict[str, int] = {}
        self._buffers: dict[str, list[dict[str, str]]] = {}
        for name, (path, _fields, key_fn) in self._specs.items():
            index, row_count = self._load_index(path, key_fn)
            self._indexes[name] = index
            self._disk_rows[name] = row_count
            self._buffers[name] = []
        self._pending = 0

    # ------------------------------------------------------------------ keys
    @staticmethod
    def _api_key(row: dict[str, str]) -> tuple[str, str]:
        return (row.get("provider", ""), row.get("cache_key", ""))

    @staticmethod
    def _path_key(row: dict[str, str]) -> str:
        return row.get("path", "")

    # ----------------------------------------------------------------- files
    @staticmethod
    def _ensure_file(path: Path, fieldnames: list[str]) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig writes a leading BOM so the file opens cleanly in Excel.
        with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

    def _load_index(
        self, path: Path, key_fn: Callable[[dict[str, str]], Any]
    ) -> tuple[dict[Any, dict[str, str]], int]:
        """Stream the file row-by-row into a last-wins index.

        Only the deduplicated index is retained; the raw rows are never all held
        in memory at once, so startup memory scales with the number of distinct
        keys rather than the (possibly much larger) row count on disk.
        """
        index: dict[Any, dict[str, str]] = {}
        row_count = 0
        if not path.exists():
            return index, row_count
        with path.open(newline="", encoding="utf-8-sig") as csv_file:
            for row in csv.DictReader(csv_file):
                index[key_fn(row)] = row
                row_count += 1
        return index, row_count

    @staticmethod
    def _write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temp_path.replace(path)

    # -------------------------------------------------------------- flushing
    def _mark_dirty_locked(self) -> None:
        self._pending += 1
        if self._pending >= self.flush_every:
            self._flush_locked()

    def _flush_locked(self) -> None:
        for name, (path, fields, _key_fn) in self._specs.items():
            buffer = self._buffers[name]
            if not buffer:
                continue
            # Plain utf-8 (no BOM) for appends: utf-8-sig would inject a stray BOM
            # in the middle of the file on every append and corrupt the CSV.
            with path.open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=fields, extrasaction="ignore")
                writer.writerows(buffer)
            self._disk_rows[name] += len(buffer)
            buffer.clear()
        self._pending = 0

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        with self._lock:
            self._flush_locked()
            if self.compact_on_close:
                self.compact(force=False)

    def compact(self, force: bool = False) -> list[str]:
        """Rewrite de-duplicated files atomically. Returns names actually compacted.

        Runs only when a file has bloated past ``compact_bloat_ratio`` (duplicate
        rows accumulated across runs) unless ``force`` is set. Never called on the
        hot write path.
        """
        compacted: list[str] = []
        with self._lock:
            self._flush_locked()
            for name, (path, fields, _key_fn) in self._specs.items():
                index = self._indexes[name]
                unique = len(index)
                disk = self._disk_rows[name]
                if not force and (unique == 0 or disk <= unique * self.compact_bloat_ratio):
                    continue
                self._write_rows(path, fields, list(index.values()))
                self._disk_rows[name] = unique
                compacted.append(name)
                LOGGER.info("compacted %s: %s rows -> %s unique", path.name, disk, unique)
        return compacted

    # ----------------------------------------------------------------- cache
    def get_cache(self, provider: str, cache_key: str, max_age_seconds: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._indexes["api_cache"].get((provider, cache_key))
        if not row:
            return None
        created_at = float(row.get("created_at") or 0)
        if time.time() - created_at > max_age_seconds:
            return None
        response_json = row.get("response_json") or "{}"
        try:
            return json.loads(response_json)
        except ValueError:
            return None

    def set_cache(self, provider: str, cache_key: str, status_code: int, response: dict[str, Any]) -> None:
        row = {
            "provider": provider,
            "cache_key": cache_key,
            "status_code": str(status_code),
            "response_json": json.dumps(response, ensure_ascii=False),
            "created_at": str(time.time()),
        }
        with self._lock:
            self._indexes["api_cache"][(provider, cache_key)] = row
            self._buffers["api_cache"].append(row)
            self._mark_dirty_locked()

    # ------------------------------------------------------------- inventory
    def upsert_media(self, media: MediaFile) -> None:
        row = {
            "path": str(media.path),
            "size_bytes": str(media.size_bytes),
            "mtime": str(media.mtime),
            "extension": media.extension,
            "duration_seconds": str(media.duration_seconds or ""),
            "bitrate": str(media.bitrate or ""),
            "tags_json": json.dumps(media.tags, ensure_ascii=False),
            "has_cover_art": "yes" if media.has_cover_art else "no",
            "scanned_at": str(time.time()),
        }
        with self._lock:
            self._indexes["media"][str(media.path)] = row
            self._buffers["media"].append(row)
            self._mark_dirty_locked()

    # ---------------------------------------------------------------- resume
    def should_skip(self, path: Path, size_bytes: int, mtime: float) -> bool:
        with self._lock:
            row = self._indexes["enrichment"].get(str(path))
        if not row:
            return False
        try:
            same_size = int(float(row.get("size_bytes") or 0)) == int(size_bytes)
            same_mtime = abs(float(row.get("mtime") or 0) - float(mtime)) < 0.001
        except ValueError:
            return False
        status = row.get("status", "")
        return same_size and same_mtime and status in self.RESUMABLE_STATUSES

    def save_enrichment(self, result: EnrichmentResult) -> None:
        row = {
            "path": str(result.media.path),
            "size_bytes": str(result.media.size_bytes),
            "mtime": str(result.media.mtime),
            "status": result.status,
            "written_fields_json": json.dumps(result.written_fields, ensure_ascii=False),
            "merged_json": json.dumps(
                {
                    "fields": result.merged.fields,
                    "field_confidence": result.merged.field_confidence,
                    "field_sources": result.merged.field_sources,
                    "providers_used": result.merged.providers_used,
                    "missing_required": result.merged.missing_required,
                    "notes": result.merged.notes,
                },
                ensure_ascii=False,
            ),
            "providers_json": json.dumps(
                [
                    {
                        "provider": provider.provider,
                        "confidence": provider.confidence,
                        "fields": provider.fields,
                        "source_url": provider.source_url,
                        "notes": provider.notes,
                        "raw": provider.raw,
                    }
                    for provider in result.provider_results
                ],
                ensure_ascii=False,
            ),
            "error": result.error,
            "processed_at": str(time.time()),
        }
        with self._lock:
            self._indexes["enrichment"][str(result.media.path)] = row
            self._buffers["enrichment"].append(row)
            self._mark_dirty_locked()
