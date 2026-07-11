# Server Deployment

## CPU Control

Set this in `.env`:

```text
TAG_CREATOR_CPU_THREADS=2
```

The tool applies this value to Python/NumPy/TensorFlow/Essentia thread environment variables:

- `OMP_NUM_THREADS`
- `OPENBLAS_NUM_THREADS`
- `MKL_NUM_THREADS`
- `NUMEXPR_NUM_THREADS`
- `TF_NUM_INTRAOP_THREADS`
- `TF_NUM_INTEROP_THREADS`

When running Docker, also set a Docker CPU quota:

```powershell
docker run --cpus=2 --env-file .env tag_creator --input-dir media --dry-run
```

## Build Without Local AI

```powershell
cd tag_creator
docker build -t tag_creator .
```

## Build With Local AI Dependencies

```powershell
cd tag_creator
docker build --build-arg INSTALL_LOCAL_AI=true -t tag_creator-ai .
```

Model files are not copied into the image. Mount them into:

```text
/app/models/local_ai
```

## Run

```powershell
docker run --rm --cpus=2 --env-file .env `
  -v ${PWD}/../ftp_downloads/mp3:/app/media:ro `
  -v ${PWD}/data:/app/data `
  -v ${PWD}/output:/app/output `
  -v ${PWD}/models/local_ai:/app/models/local_ai:ro `
  tag_creator --input-dir media --report output/enrichment_report.csv --dry-run
```

## Readiness Check

```powershell
python scripts\server_readiness.py
python scripts\verify_api_keys.py
python scripts\verify_local_ai.py
```

## Production Run Rule

Start with dry-run reports. Only run `--write` against copied files after reviewing:

- `missing_required`
- `field_confidence_json`
- `field_sources_json`
- `notes`
