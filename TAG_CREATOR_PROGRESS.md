# Tag Creator Progress

Status: CSV-only hybrid implementation pass completed, audited, improved, and sample-tested.

Resume command from workspace root:

```powershell
cd tag_creator
python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --limit 2 --dry-run
```

If the user says "Start work from where it stopped", continue from this file:

1. Check `git status --short` and `tag_creator/`.
2. Run `python -m py_compile` against the package and scripts.
3. Run a dry-run enrichment on 1-2 files.
4. If tests pass, continue expanding provider behavior or run larger batches.

Implementation checklist:

- [x] Project directory and configuration files.
- [x] Core scanner/cache/media/tag writer.
- [x] Provider clients.
- [x] Enrichment pipeline.
- [x] Local sample test.
- [x] SQLite removed from code path.
- [x] CSV-only retest after storage replacement.
- [x] SONOTELLER/RapidAPI adapter added as optional paid provider.
- [x] Paid API sample-call docs added.
- [x] Hybrid three-stage pipeline implemented.
- [x] Free providers added: iTunes, Deezer, Wikidata.
- [x] Safe web discovery provider added with allowlist and robots checks.
- [x] Local rules inference provider added for low-confidence playlist taxonomy hints.
- [x] Audit improvement pass completed:
  - CSV store now uses in-memory indexes plus periodic flush for large runs.
  - HTTP providers now retry/back off on rate limits and temporary server errors.
  - Added `local_cleanup` stage for YouTube-style filenames/titles before API search.
  - Added stricter match validation for Spotify, Last.fm, Genius, Discogs, Deezer, iTunes, and web discovery.
  - Merge layer now normalizes equivalent dates, years, track numbers, disc numbers, and BPM values before scoring.
- [x] Optional local open-source AI audio model foundation added:
  - `essentia_discogs_effnet` provider for Discogs-EffNet style genre/style descriptors.
  - `musicnn_mtg_jamendo` provider for MTG-Jamendo/MusicNN mood/theme/instrument descriptors.
  - Local AI runs after official/free APIs and before web/paid fallback when enabled.
  - Large model files are external to git under `models/local_ai/`.
  - Missing dependencies/model files skip cleanly instead of breaking a batch.
- [x] Server hardening pass:
  - Added CPU/thread controls via `TAG_CREATOR_CPU_THREADS`.
  - Added Dockerfile, `.dockerignore`, compose example, and server deployment docs.
  - Added `server_readiness.py` for path/provider/resource checks.
  - Raised local AI provider confidence/weights so verified predictions can pass the configured merge threshold.

Current test results:

- SQLite-backed implementation was removed.
- Current work replaced SQLite with CSV state files:
  - `data/api_cache.csv`
  - `data/media_inventory.csv`
  - `data/enrichment_state.csv`
- CSV-only commands tested:
  - `python scripts\verify_api_keys.py`
  - `python scripts\export_tags_csv.py --input-dir ..\ftp_downloads\mp3 --output output\existing_tags_csv_store_sample.csv --preset full --limit 2`
  - `python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --report output\enrichment_csv_store_sample.csv --limit 2 --dry-run --no-resume`
- Multi-provider conflict notes are now written into report `notes`.
- Hybrid dry-run tested:
  - `python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --report output\hybrid_free_web_rules_sample.csv --limit 2 --dry-run --no-resume`
- Audit dry-run tested:
  - `python -m py_compile (Get-ChildItem ..\tag_creator -Recurse -Filter *.py | ForEach-Object { $_.FullName })`
  - `python scripts\verify_api_keys.py`
  - `python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --report output\audit_improved_sample_v3.csv --limit 2 --dry-run --no-resume`
  - `python scripts\build_paid_candidates.py --report output\audit_improved_sample_v3.csv --output output\audit_paid_candidates_sample_v3.csv`

Current provider status with blank `.env` keys:

- Active: local cleanup, iTunes, Deezer, Wikidata, web discovery, rules inference, MusicBrainz, Cover Art Archive.
- Active after provided keys: Spotify, Last.fm, Discogs.
- Waiting for API keys/tools: SONOTELLER, AcoustID, Genius.
- Waiting for optional local model install: Essentia/Discogs-EffNet and MusicNN/MTG-Jamendo model files.

Next recommended tasks:

1. Add API keys in `.env`.
2. For SONOTELLER, make owned files reachable by URL and set `SONOTELLER_FILE_URL_BASE`.
3. Run `python scripts\show_paid_api_samples.py`.
4. Run `python scripts\verify_api_keys.py`.
5. Run dry-run on 20 files: `python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --report output\enrichment_20.csv --limit 20 --dry-run --no-resume`.
6. Review `missing_required`, `notes`, and `field_confidence_json`.
7. Only then run `--write` on copied files first.
8. To enable local AI, install `requirements-ai.txt`, place model files under `models/local_ai/`, then set `LOCAL_AI_ENABLED=true`.
9. For server use, set `TAG_CREATOR_CPU_THREADS`, run `scripts\server_readiness.py`, then start with dry-run reports.

## Production-hardening pass (2026-07-13)

Focus: make everything strong, especially the local open-source AI models.

- **Config footgun fixed.** `ENABLED_PROVIDERS` now defaults to the UNION of all
  stage lists, so a blank `.env` can never silently starve the free/web stages
  while leaving the paid stage on. Added a startup warning when a stage names a
  provider that is not enabled. (`config.py`)
- **Local AI mapping rewritten for full value capture (the key change).**
  `local_ai_audio._field_map` is now taxonomy/head-driven instead of a tiny
  keyword allowlist. Every real prediction is routed to the correct field:
  Discogs genre400 `Parent---Child` -> genre + subgenre/style; mtg_jamendo
  mood/theme head -> mood/moods/themes/occasion/season; mtg_jamendo instrument
  head -> instruments/vocals; MSD 50-tag autotagger -> classified by label.
  Previously-dropped values (e.g. genre "Trap", mood "epic") are now kept.
- **Extra heads enabled out of the box.** `download_models.py` now fetches the
  mood/theme + instrument heads by default, and `.env.example` sets
  `ESSENTIA_EXTRA_HEADS` so the Discogs-EffNet provider fills genre + subgenre +
  mood/theme + instruments in one embedding pass. (Set `ESSENTIA_EXTRA_HEADS` in
  your real `.env` — it is not auto-edited.)
- **csv_store startup is now truly streaming** (bounded memory on large caches).
- **Packaging:** `pyproject.toml` uses `packages.find` (no manual list to drift).
- **Tests: 20 -> 49, all passing offline (no network, no essentia, no real audio).**
  New: `test_local_ai_mapping` (proves 100% field capture), `test_pipeline`
  (merge/resume/PaidGuard/streaming report), `test_http_base` (retry/404-cache/
  circuit breaker), `test_media` (tag write round-trip/atomic/backup/verify),
  `test_reports` (streaming + resume), `test_config_providers` (enable-union +
  warning). Shared hermetic fixtures in `tests/conftest.py`.
- **CI added:** `.github/workflows/tag_creator-ci.yml` (byte-compile + offline
  pytest; no secrets/network).

Server validation still to run by owner: install `essentia-tensorflow` on a
pinned Python 3.11/3.12, `python scripts/download_models.py`, set
`LOCAL_AI_ENABLED=true` + `ESSENTIA_EXTRA_HEADS`, then a dry-run on ~20 files and
review genre/subgenre/mood/moods/themes/instruments/vocals/bpm/key in the report.
