# Local AI Audio Models

`tag_creator` can use optional open-source audio models after official metadata APIs and before paid AI providers.

## What These Models Do

They analyze the audio file itself and predict descriptive tags:

- genre / style hints
- mood / theme hints
- instruments / vocal hints
- playlist suitability descriptors

They do **not** reliably identify exact factual metadata such as artist, title, album, ISRC, label, copyright, or release year. Those fields should still come from Spotify, Last.fm, Discogs, MusicBrainz, Deezer, iTunes, AcoustID, and similar catalog sources.

## Supported Providers

### `essentia_features` (no model files required)

Pure-DSP descriptors computed directly by Essentia — **no downloaded model files**,
only the `essentia-tensorflow` package. This is the fastest path to filling the
fields that otherwise force the paid stage:

- `bpm` (RhythmExtractor2013)
- `key` + scale (KeyExtractor, e.g. `C# minor`)
- `danceability` (normalized 0-1)

Each extractor degrades independently: if one fails the others still return. This
provider is enabled by `LOCAL_AI_ENABLED=true` alone (no `.pb` files needed).

### `essentia_discogs_effnet`

Uses an Essentia Discogs-EffNet style embedding model plus a prediction head.

Best for:

- genre
- style/subgenre
- broad music descriptors

Expected files:

```text
tag_creator/models/local_ai/discogs-effnet-embeddings.pb
tag_creator/models/local_ai/genre_discogs400-discogs-effnet.pb
tag_creator/models/local_ai/discogs-effnet-labels.txt
```

### `musicnn_mtg_jamendo`

Uses an Essentia/MusicNN model trained for MTG-Jamendo style music tagging.

Best for:

- mood
- theme
- instruments
- playlist suitability

Expected files:

```text
tag_creator/models/local_ai/mtg_jamendo_musicnn.pb
tag_creator/models/local_ai/mtg_jamendo_labels.txt
```

### `clap_zero_shot`

Uses LAION CLAP through Hugging Face Transformers for heavier zero-shot audio
tagging. It scores the audio against configurable labels such as `genre: house`,
`subgenre: dance pop`, `mood: energetic`, `occasion: retail background`,
`weather: sunny`, `age_group: youth`, and `instrument: piano`.

Best for:

- mood / energy / occasion / theme
- broad genre confirmation
- instrument/vocal hints

It does **not** identify factual catalog metadata such as title, artist, album,
release year, ISRC, copyright, or lyrics.

Model files are downloaded once into the mounted local-AI directory:

```text
tag_creator/models/local_ai/hf/
```

On the server this should be mounted from:

```text
D:\editorBackend\tag_ai
```

Docker download command:

```powershell
docker run --rm --user root --entrypoint python --env-file .env `
  -v D:/editorBackend/tag_ai:/app/models/local_ai `
  tag_creator:local-ai scripts/download_hf_models.py
```

### Extra prediction heads (shared embedding)

`essentia_discogs_effnet` computes the Discogs-EffNet embedding once and can run
additional prediction heads off it (mood/theme, instrument, ...) without
recomputing audio features. Configure heads in `.env` as
`model|labels[|output_node]`, comma-separated:

```text
ESSENTIA_EXTRA_HEADS=models/local_ai/mtg_jamendo_moodtheme-discogs-effnet.pb|models/local_ai/mtg_jamendo_moodtheme-labels.txt,models/local_ai/mtg_jamendo_instrument-discogs-effnet.pb|models/local_ai/mtg_jamendo_instrument-labels.txt
```

A head that fails to load is skipped; the genre head and the rest still run.

## Download Models

Fetch the open Essentia model-zoo files (labels are extracted from each model's
metadata JSON) into `models/local_ai/`:

```powershell
python scripts\download_models.py --list
python scripts\download_models.py                    # core: embeddings, genre, musicnn
python scripts\download_models.py --only moodtheme_head instrument_head
```

Downloads are resumable and verified against SHA256 when a checksum is pinned in
the script or in `models/local_ai/CHECKSUMS.txt` (`<sha256>  <filename>` per line).

## Setup

Install the normal dependencies first:

```powershell
pip install -r requirements.txt
```

Install optional local AI dependencies only on the machine that will run local model inference:

```powershell
pip install -r requirements-ai.txt
```

If Windows cannot install `essentia-tensorflow`, run this layer on a Linux VM/server and keep the rest of the CSV workflow unchanged.

## Enable In `.env`

```text
LOCAL_AI_ENABLED=true
LOCAL_AI_STAGE_PROVIDERS=essentia_features,essentia_discogs_effnet,musicnn_mtg_jamendo,clap_zero_shot
LOCAL_AI_TOP_N=12
LOCAL_AI_MIN_SCORE=0.12
CLAP_MODEL_NAME=laion/clap-htsat-unfused
CLAP_CACHE_DIR=models/local_ai/hf
```

`essentia_features` needs only `LOCAL_AI_ENABLED=true`; the `.pb`-based providers
also need their model files (see Download Models).

Keep `LOCAL_AI_ENABLED=false` until the dependencies and model files are installed.

## Pipeline Order

```text
Stage 0: local filename cleanup
Stage 1: official/free catalog APIs
Stage 2: local open-source AI audio models
Stage 2b: safe web discovery, if still enabled
Stage 3: paid AI fallback, only if important tags are still missing
```

## Batch Safety

- Results are cached in CSV by file path, file size, mtime, model path, top-N, and threshold.
- Missing dependencies or model files do not crash the batch; providers are marked not configured.
- The runner executes in a subprocess to isolate heavy TensorFlow/Essentia loading.
- Low-score predictions are kept out of final fields but remain visible in `analysis_json`.

## Test

```powershell
python scripts\verify_api_keys.py
python scripts\download_hf_models.py
python scripts\enrich_library.py --input-dir ..\ftp_downloads\mp3 --limit 1 --dry-run --no-resume
```

The providers should show `configured=yes` only after `LOCAL_AI_ENABLED=true`, `essentia-tensorflow` is installed, and model files exist.

## Official References

- Essentia model index: `https://essentia.upf.edu/models.html`
- MTG-Jamendo dataset: `https://mtg.github.io/mtg-jamendo-dataset/`
