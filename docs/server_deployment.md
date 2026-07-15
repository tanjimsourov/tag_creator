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

On the current Windows server, keep the downloaded open-source model files here:

```text
D:\editorBackend\tag_ai
```

Keep this in `.env`:

```text
LOCAL_AI_HOST_DIR=D:/editorBackend/tag_ai
LOCAL_AI_MODELS_DIR=models/local_ai
```

## Run

```powershell
docker run --rm --cpus=2 --env-file .env `
  -v ${PWD}/../ftp_downloads/mp3:/app/media:ro `
  -v ${PWD}/data:/app/data `
  -v ${PWD}/output:/app/output `
  -v D:/editorBackend/tag_ai:/app/models/local_ai:ro `
  tag_creator --input-dir media --report output/enrichment_report.csv --dry-run
```

For cache-free test runs, use temporary in-memory data/log mounts:

```powershell
docker run --rm --user root --cpus=2 --env-file .env `
  --tmpfs /app/data --tmpfs /app/logs `
  -v ${PWD}/test_media:/app/test_media:ro `
  -v ${PWD}/output:/app/output `
  -v D:/editorBackend/tag_ai:/app/models/local_ai:ro `
  tag_creator:local-ai --input-dir test_media/free_ai_5 --report output/free_ai_5_docker_ai_report.csv --limit 5 --dry-run --no-resume
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
