# Tag Creator

Professional metadata enrichment pipeline for large MP3/MP4 libraries.

The tool reads existing tags, cleans local/YouTube-style filenames, searches multiple metadata providers, verifies fields with confidence scoring, writes only high-confidence metadata, and exports CSV/JSON reports. It is designed to resume safely for thousands of files.

All cache, inventory, and resume state is stored in CSV files. The project does not use SQLite.

## Important Limits

No tool can guarantee every tag for every song. The pipeline fills verified fields and reports missing or low-confidence fields. It does not invent composer, ISRC, genre, BPM, lyrics, or copyright values when sources do not provide them.

## Setup

```powershell
cd tag_creator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Edit `.env` and add any API keys you have:

- `ACOUSTID_API_KEY`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `LASTFM_API_KEY`
- `DISCOGS_TOKEN`
- `GENIUS_ACCESS_TOKEN`

MusicBrainz and Cover Art Archive do not need keys.

The built-in `local_cleanup` provider does not need a key. It improves provider searches by cleaning filenames and obvious video-title noise before external lookups.

## Safe Test

Dry-run enrichment on two files:

```powershell
python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --limit 2 --dry-run
```

Export existing tags:

```powershell
python scripts\export_tags_csv.py --input-dir ..\ftp_downloads\mp3 --output output\existing_tags.csv --preset full
```

Write tags only when ready:

```powershell
python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --write --limit 2
```

## Resume

The pipeline stores API cache and per-file status in CSV files inside `data/`.

If work stops, run the same command again. With `RESUME=true`, already-completed files with unchanged path/mtime/size are skipped.

CSV state is flushed during long runs, not only at the end, so large batches can resume more safely after interruption.

## Scripts

- `scripts/scan_library.py` - scan files and save inventory.
- `scripts/enrich_library.py` - search providers, score metadata, optionally write tags.
- `scripts/export_tags_csv.py` - export tags already inside media files.
- `scripts/verify_api_keys.py` - check which providers are enabled/configured.
- `scripts/verify_live_providers.py` - make small live calls to validate enabled live credentials.
- `scripts/verify_local_ai.py` - check local AI dependencies and model files.
- `scripts/server_readiness.py` - check server paths, CPU settings, and provider readiness.
- `scripts/show_paid_api_samples.py` - print configured paid API sample calls.
- `scripts/build_paid_candidates.py` - filter free/web report to only files that still need paid AI analysis.

## Optional Local AI Audio Models

`tag_creator` can run open-source local audio models after official APIs and before paid AI fallback:

- `essentia_features` for BPM, key/scale, and danceability.
- `essentia_discogs_effnet` for genre/style descriptors.
- `musicnn_mtg_jamendo` for mood/theme/instrument-style descriptors.
- `clap_zero_shot` for broader genre, subgenre, mood, energy, occasion, weather/season, age-group, instrument, and vocal descriptors.

These models analyze the audio file directly. They help with advanced playlist tags, but they do not replace factual catalog sources for artist, title, album, ISRC, label, or year.

Keep `LOCAL_AI_ENABLED=false` until optional dependencies and model files are installed. On the server, use `D:\editorBackend\tag_ai` as the mounted model folder and run Docker with the `tag_creator:local-ai` image.

```powershell
pip install -r requirements-ai.txt
```

More notes: `docs/local_ai_models.md`.

## Server / Docker

Set CPU usage in `.env`:

```text
TAG_CREATOR_CPU_THREADS=2
```

Build and run:

```powershell
docker build -t tag_creator .
docker run --rm --cpus=2 --env-file .env tag_creator --input-dir media --dry-run
```

More notes: `docs/server_deployment.md`.

## Paid API Integration

SONOTELLER/RapidAPI is supported as a paid AI-analysis provider through `.env`.
It is paused by default. Enable it only when paid processing is approved by adding
`sonoteller` to `PAID_STAGE_PROVIDERS`.

```text
SONOTELLER_RAPIDAPI_KEY=
SONOTELLER_RAPIDAPI_HOST=sonoteller-ai1.p.rapidapi.com
SONOTELLER_BASE_URL=https://sonoteller-ai1.p.rapidapi.com
SONOTELLER_ANALYZE_ENDPOINT=/music
SONOTELLER_FILE_URL_BASE=
```

More notes: `docs/paid_api_integration_notes.md`.

## Hybrid Cost-Saving Pipeline

The default enrichment mode is hybrid:

```text
Stage 0: local cleanup for better search queries
Stage 1: free/catalog APIs
Stage 2: safe web discovery for missing fields
Stage 3: optional paid SONOTELLER only if explicitly enabled
```

Details: `docs/hybrid_pipeline.md`.

## Output

- `output/enrichment_report.csv` - per-file enrichment result.
- `output/enrichment_report.jsonl` - detailed provider/source data.
- `data/api_cache.csv` - provider response cache.
- `data/media_inventory.csv` - scanned file inventory.
- `data/enrichment_state.csv` - per-file resume status.
