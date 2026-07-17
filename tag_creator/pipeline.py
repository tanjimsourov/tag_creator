from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from tqdm import tqdm

from .clients.cover_art_archive import CoverArtArchiveClient
from .config import Settings
from .csv_store import CsvStore
from .media import read_media_file, scan_media_files, write_tags
from .models import EnrichmentResult, MediaFile, ProviderResult, RunSummary
from .providers import build_provider_client_map
from .rate_limit import RateLimiter
from .reports import StreamingReportWriter, write_run_summary
from .scoring import fields_to_write, merge_metadata

LOGGER = logging.getLogger(__name__)


class PaidGuard:
    """Thread-safe hard cap on paid-provider invocations for a whole run."""

    def __init__(self, max_calls: int | None) -> None:
        self.max_calls = max_calls
        self.count = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        if self.max_calls is None:
            return True
        with self._lock:
            if self.count >= self.max_calls:
                return False
            self.count += 1
            return True


def _cover_art_result(clients: list, provider_results: list[ProviderResult]) -> ProviderResult | None:
    cover_client = next((client for client in clients if isinstance(client, CoverArtArchiveClient)), None)
    if not cover_client:
        return None
    release_ids = [
        result.raw.get("release_mbid", "")
        for result in provider_results
        if result.raw.get("release_mbid")
    ]
    for release_mbid in release_ids:
        result = cover_client.enrich_by_release(release_mbid)
        if result and result.fields:
            return result
    return None


def _run_clients(media: MediaFile, clients: list, stage_name: str) -> list[ProviderResult]:
    provider_results: list[ProviderResult] = []
    for client in clients:
        if isinstance(client, CoverArtArchiveClient):
            continue
        start = time.monotonic()
        try:
            result = client.enrich(media)
        except Exception as exc:
            LOGGER.exception("%s failed for %s", client.provider_name, media.path)
            result = ProviderResult(client.provider_name, 0, {}, notes=f"error: {exc}")
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        if result:
            result.notes = f"{stage_name}: {result.notes}" if result.notes else stage_name
            result.raw = {**(result.raw or {}), "_elapsed_ms": elapsed_ms}
            provider_results.append(result)
    return provider_results


def _select_clients(client_map: dict, names: list[str]) -> list:
    return [client_map[name] for name in names if name in client_map]


def _with_provider_fields(media: MediaFile, provider_results: list[ProviderResult]) -> MediaFile:
    tags = dict(media.tags)
    for result in provider_results:
        if result.confidence < 0.60:
            continue
        for field in ("title", "artist", "album", "album_artist", "year", "date"):
            value = result.fields.get(field, "").strip()
            if value:
                tags[field] = value
    return replace(media, tags=tags)


def enrich_one(
    media: MediaFile,
    client_map: dict,
    settings: Settings,
    paid_guard: PaidGuard | None = None,
) -> EnrichmentResult:
    provider_results: list[ProviderResult] = []
    error = ""

    if settings.hybrid_mode:
        local_clients = _select_clients(client_map, ["local_cleanup"])
        local_results = _run_clients(media, local_clients, "stage0_prepare")
        provider_results.extend(local_results)
        prepared_media = _with_provider_fields(media, local_results)

        free_client_names = [name for name in settings.free_stage_providers if name != "local_cleanup"]
        free_clients = _select_clients(client_map, free_client_names)
        provider_results.extend(_run_clients(prepared_media, free_clients, "stage1_free"))

        interim = merge_metadata(
            media=media,
            results=provider_results,
            provider_weights=settings.provider_weights,
            min_field_confidence=settings.min_field_confidence,
            required_tags=settings.required_tags,
        )

        if interim.missing_required:
            # Second catalog pass: if AcoustID/iTunes/Deezer found cleaner
            # title/artist data than the original file/filename, let high-trust
            # catalog providers verify and enrich from that better identity.
            stage1_verify_media = replace(media, tags={**media.tags, **interim.fields})
            verifier_names = [
                name
                for name in ("musicbrainz", "discogs", "wikidata", "lastfm", "cover_art_archive")
                if name in settings.free_stage_providers
            ]
            provider_results.extend(
                _run_clients(
                    stage1_verify_media,
                    _select_clients(client_map, verifier_names),
                    "stage1_verify",
                )
            )

        interim = merge_metadata(
            media=media,
            results=provider_results,
            provider_weights=settings.provider_weights,
            min_field_confidence=settings.min_field_confidence,
            required_tags=settings.required_tags,
        )

        if settings.local_ai_stage_providers and (interim.missing_required or settings.local_ai_always_run):
            stage2_media = replace(media, tags={**media.tags, **interim.fields})
            local_ai_clients = _select_clients(client_map, settings.local_ai_stage_providers)
            provider_results.extend(_run_clients(stage2_media, local_ai_clients, "stage2_local_ai"))

        interim = merge_metadata(
            media=media,
            results=provider_results,
            provider_weights=settings.provider_weights,
            min_field_confidence=settings.min_field_confidence,
            required_tags=settings.required_tags,
        )

        if interim.missing_required and settings.web_stage_providers:
            stage2_media = replace(media, tags={**media.tags, **interim.fields})
            web_clients = [
                client
                for client in _select_clients(client_map, settings.web_stage_providers)
                if client.provider_name != "rules_inference"
            ]
            provider_results.extend(_run_clients(stage2_media, web_clients, "stage2_web"))

        interim = merge_metadata(
            media=media,
            results=provider_results,
            provider_weights=settings.provider_weights,
            min_field_confidence=settings.min_field_confidence,
            required_tags=settings.required_tags,
        )

        rules_clients = _select_clients(client_map, ["rules_inference"])
        if rules_clients:
            stage2_media = replace(media, tags={**media.tags, **interim.fields})
            provider_results.extend(_run_clients(stage2_media, rules_clients, "stage2_rules"))

        interim = merge_metadata(
            media=media,
            results=provider_results,
            provider_weights=settings.provider_weights,
            min_field_confidence=settings.min_field_confidence,
            required_tags=settings.required_tags,
        )

        if settings.paid_stage_providers and (interim.missing_required or not settings.paid_only_if_missing):
            if paid_guard is None or paid_guard.try_acquire():
                stage3_media = replace(media, tags={**media.tags, **interim.fields})
                paid_clients = _select_clients(client_map, settings.paid_stage_providers)
                provider_results.extend(_run_clients(stage3_media, paid_clients, "stage3_paid"))
            else:
                LOGGER.info("paid stage skipped for %s (MAX_PAID_CALLS reached)", media.path.name)
    else:
        provider_results.extend(_run_clients(media, list(client_map.values()), "single_stage"))

    cover_result = _cover_art_result(list(client_map.values()), provider_results)
    if cover_result:
        cover_result.notes = f"stage1_free: {cover_result.notes}"
        provider_results.append(cover_result)

    merged = merge_metadata(
        media=media,
        results=provider_results,
        provider_weights=settings.provider_weights,
        min_field_confidence=settings.min_field_confidence,
        required_tags=settings.required_tags,
    )
    writable = fields_to_write(media, merged, settings.min_write_confidence)
    written_fields: list[str] = []
    status = "dry_run_done"

    if not writable:
        status = "no_change"
    if settings.write_tags and not settings.dry_run and writable:
        try:
            written_fields = write_tags(
                media.path,
                writable,
                settings.write_cover_art,
                backup_dir=settings.backup_dir,
                input_root=settings.input_dir,
                verify=settings.verify_after_write,
            )
            status = "updated" if written_fields else "no_change"
        except Exception as exc:
            LOGGER.exception("writing failed for %s", media.path)
            status = "write_failed"
            error = str(exc)

    return EnrichmentResult(
        media=media,
        merged=merged,
        status=status,
        written_fields=written_fields,
        provider_results=provider_results,
        error=error,
    )


def scan_library(settings: Settings, store: CsvStore, input_dir: Path | None = None, limit: int | None = None) -> list[MediaFile]:
    selected_input = input_dir or settings.input_dir
    selected_limit = settings.limit if limit is None else limit
    paths = scan_media_files(selected_input, settings.supported_extensions, selected_limit)
    media_files: list[MediaFile] = []
    for path in tqdm(paths, desc="Scanning media"):
        try:
            media = read_media_file(path)
            store.upsert_media(media)
            media_files.append(media)
        except Exception as exc:
            LOGGER.warning("scan failed for %s: %s", path, exc)
    return media_files


def _safe_enrich_one(
    media: MediaFile, client_map: dict, settings: Settings, paid_guard: PaidGuard | None
) -> EnrichmentResult:
    """enrich_one wrapped so a worker thread never raises to the executor."""
    try:
        return enrich_one(media, client_map, settings, paid_guard=paid_guard)
    except Exception as exc:  # noqa: BLE001 - isolate a single bad file
        LOGGER.exception("enrichment failed for %s", media.path)
        merged = merge_metadata(
            media, [], settings.provider_weights, settings.min_field_confidence, settings.required_tags
        )
        return EnrichmentResult(media, merged, "failed", [], [], str(exc))


def _record_metrics(
    summary: RunSummary,
    result: EnrichmentResult,
    latency_sum: dict[str, float],
    latency_count: dict[str, int],
) -> None:
    summary.status_counts[result.status] = summary.status_counts.get(result.status, 0) + 1
    for provider in result.provider_results:
        name = provider.provider
        if provider.fields:
            summary.provider_hits[name] = summary.provider_hits.get(name, 0) + 1
        if provider.notes and "error:" in provider.notes:
            summary.provider_errors[name] = summary.provider_errors.get(name, 0) + 1
        elapsed = provider.raw.get("_elapsed_ms") if isinstance(provider.raw, dict) else None
        if isinstance(elapsed, (int, float)):
            latency_sum[name] = latency_sum.get(name, 0.0) + float(elapsed)
            latency_count[name] = latency_count.get(name, 0) + 1


def enrich_library(
    settings: Settings,
    store: CsvStore,
    input_dir: Path | None = None,
    report_csv: Path | None = None,
    limit: int | None = None,
    final_csv: bool = False,
    debug_output: bool = True,
) -> RunSummary:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    client_map = build_provider_client_map(settings, store, RateLimiter(settings.rate_limits))
    media_files = scan_library(settings, store, input_dir=input_dir, limit=limit)

    # Filter resume-skips up front so we never even schedule already-done files.
    pending = [
        media
        for media in media_files
        if not (settings.resume and store.should_skip(media.path, media.size_bytes, media.mtime))
    ]
    skipped = len(media_files) - len(pending)
    if skipped:
        LOGGER.info("resume: skipping %s already-processed files", skipped)

    workers = max(1, settings.worker_threads)
    report_path = report_csv or settings.report_csv
    summary = RunSummary(
        report_path=str(report_path),
        total_files=len(media_files),
        skipped=skipped,
        workers=workers,
    )
    processed: set = set()
    paid_guard = PaidGuard(settings.max_paid_calls)
    latency_sum: dict[str, float] = {}
    latency_count: dict[str, int] = {}
    LOGGER.info("enriching %s files with %s worker(s)", len(pending), workers)

    writer = StreamingReportWriter(
        report_path,
        append=settings.resume,
        final_csv=final_csv,
        write_jsonl=debug_output,
        final_no_blanks=settings.final_no_blanks,
        final_missing_value=settings.final_missing_value,
    )
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {
        executor.submit(_safe_enrich_one, media, client_map, settings, paid_guard): media for media in pending
    }

    def _consume(result: EnrichmentResult) -> None:
        store.save_enrichment(result)
        if writer.write(result):
            summary.written_rows += 1
        _record_metrics(summary, result, latency_sum, latency_count)

    try:
        for future in tqdm(as_completed(futures), total=len(futures), desc="Enriching media"):
            _consume(future.result())
            processed.add(future)
    except KeyboardInterrupt:
        summary.interrupted = True
        LOGGER.warning("interrupted — cancelling queued files and finishing in-flight work...")
        # Cancel not-yet-started work; running files are allowed to finish below.
        for future in futures:
            if future not in processed:
                future.cancel()
        for future in futures:
            if future in processed or future.cancelled():
                continue
            try:
                result = future.result()
            except (CancelledError, Exception):  # noqa: BLE001
                continue
            _consume(result)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        writer.close()
        store.flush()

    summary.paid_calls = paid_guard.count
    web_client = client_map.get("web_discovery")
    summary.web_fetches = int(getattr(web_client, "fetches", 0)) if web_client else 0
    summary.provider_latency_ms = {
        name: round(latency_sum[name] / latency_count[name], 1)
        for name in latency_sum
        if latency_count.get(name)
    }
    summary.duration_seconds = round(time.monotonic() - started, 3)

    if debug_output:
        try:
            write_run_summary(settings.output_dir / "run_summary.json", summary)
        except OSError as exc:
            LOGGER.warning("could not write run_summary.json: %s", exc)

    if summary.interrupted:
        LOGGER.warning("run ended early after %s files; rerun the same command to resume", summary.written_rows)
    return summary
