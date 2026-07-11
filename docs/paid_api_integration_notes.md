# Paid API Integration Notes

## Recommended Architecture

Use paid AI music-analysis APIs as data sources inside `tag_creator`, not as a replacement for `tag_creator`.

```text
Music files
-> tag_creator batch pipeline
-> paid AI provider, e.g. SONOTELLER
-> conflict/quality checks
-> CSV state + enrichment reports
-> Local Hero DB / playlist automation
```

## SONOTELLER / RapidAPI

Public information confirms that SONOTELLER can analyze owned music files and produce rich descriptors useful for Local Hero:

- genres and subgenres
- moods
- instruments
- BPM and key
- lyrics summary
- themes
- language
- explicit-content flag
- song sections / golden minute

SONOTELLER states that YouTube is for demo use only and API use should analyze owned music files. Their public site also says the API has dedicated endpoints for music, lyrics analysis, and sections tagging, and can sync with catalog workflows.

### Current `.env` Fields

```text
SONOTELLER_RAPIDAPI_KEY=
SONOTELLER_RAPIDAPI_HOST=sonoteller-ai1.p.rapidapi.com
SONOTELLER_BASE_URL=https://sonoteller-ai1.p.rapidapi.com
SONOTELLER_ANALYZE_ENDPOINT=/music
SONOTELLER_INPUT_MODE=url
SONOTELLER_FILE_URL_BASE=
```

### Sample RapidAPI Call

Public RapidAPI-style examples use `X-RapidAPI-Key`, `X-RapidAPI-Host`, and a JSON payload containing a public audio URL.

```bash
curl --request POST \
  --url "https://sonoteller-ai1.p.rapidapi.com/music" \
  --header "Content-Type: application/json" \
  --header "X-RapidAPI-Key: YOUR_RAPIDAPI_KEY" \
  --header "X-RapidAPI-Host: sonoteller-ai1.p.rapidapi.com" \
  --data "{\"file\":\"https://your-server.example.com/audio/song.mp3\"}"
```

### Important Implementation Note

The current adapter uses URL mode because the public RapidAPI examples indicate a `file` URL payload. This means local files must be available through a controlled server/CDN URL before analysis:

```text
SONOTELLER_FILE_URL_BASE=https://your-server.example.com/audio
```

Then:

```text
C:\Music\song.mp3 -> https://your-server.example.com/audio/song.mp3
```

If the paid RapidAPI subscription exposes multipart upload instead, keep the same provider module and change only the request method after confirming the endpoint contract.

## Other Paid/Keyed Providers

These are already modeled as optional providers in `.env`:

- Spotify: track/release date/artwork/popularity/ISRC, needs developer credentials.
- Last.fm: genre/top tags, needs API key.
- Discogs: release/label/catalog metadata, needs token.
- AcoustID: fingerprint matching, needs API key and `fpcalc`.
- Genius: safe metadata/search only; lyrics scraping is intentionally disabled.

## Rules for Production Use

1. Do not write low-confidence tags.
2. Keep provider raw JSON in `analysis_json` for auditability.
3. Store all cache/state in CSV only.
4. Respect provider rate limits.
5. Do not scrape copyrighted lyrics.
6. Treat AI tags as playlist descriptors, not always canonical music metadata.
7. Use dry-run reports before writing tags or updating Local Hero DB.

## Sources Checked

- SONOTELLER official site: `https://sonoteller.ai/`
- SONOTELLER RapidAPI listing: `https://rapidapi.com/sonoteller1-sonoteller-default/api/sonoteller-ai1`

