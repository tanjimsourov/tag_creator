# Hybrid Pipeline

`tag_creator` uses a staged pipeline to reduce paid API usage.

## Stage 0: Local Cleanup

Runs before external APIs. It cleans local/YouTube-style filenames and embedded titles, for example `Artist - Song [Official Video]`.

Best for:

- improving search queries
- removing video/noise words
- parsing artist/title from filenames

This stage is not treated as a final authority by itself; it helps other providers find better matches.

## Stage 1: Free / Catalog APIs

Runs configured free or key-based catalog providers first:

- Local cleanup output for improved queries
- iTunes Search API
- Deezer public API
- MusicBrainz
- Cover Art Archive
- Spotify, if credentials are available
- Last.fm, if API key is available
- Discogs, if token is available
- AcoustID, if API key and fpcalc are available
- Wikidata metadata search

Best for:

- title
- artist
- album
- year/date
- ISRC
- label/catalog
- cover art
- broad genre hints

## Stage 2: Local Open-Source AI

Runs only if required fields remain missing, or if `LOCAL_AI_ALWAYS_RUN=true`.

Currently supported:

- `essentia_discogs_effnet`
- `musicnn_mtg_jamendo`

Best for:

- genre/style hints
- mood/theme hints
- instruments/vocal hints
- playlist suitability descriptors

These models analyze owned MP3/MP4 files locally. They do not provide reliable artist/title/album/ISRC/label metadata.

## Stage 2b: Safe Web Discovery

Runs only if required fields remain missing and web discovery is enabled.

The web provider:

- searches public web pages
- uses an allowlist from `.env`
- checks robots.txt
- extracts JSON-LD/meta/page text metadata
- does not scrape lyrics
- does not bypass paywalls or logins

Use this for possible extra fields like:

- BPM
- key
- energy
- danceability
- mood
- subgenre

## Stage 3: Paid AI Analysis

Runs only if required fields remain missing after stages 1 and 2.

Currently supported:

- SONOTELLER / RapidAPI

Best for:

- genre
- subgenre
- mood
- BPM
- key
- language
- instruments
- sections
- themes
- playlist suitability

## Important Rule

The tool must not fake tags. If sources disagree or confidence is low, the field stays missing and the CSV report explains why.

The merge layer also normalizes equivalent values, such as ISO dates and track numbers, so two providers are not treated as conflicting only because they use different formats.

## Key Output Columns

- `missing_required`
- `providers_used`
- `field_confidence_json`
- `field_sources_json`
- `hybrid_stage_summary`
- `notes`

## Build Paid Candidate List

After a dry-run report:

```powershell
python scripts\build_paid_candidates.py --report output\hybrid_free_web_rules_sample.csv --output output\paid_candidates.csv
```

This creates a smaller paid-processing list based on missing advanced fields.
