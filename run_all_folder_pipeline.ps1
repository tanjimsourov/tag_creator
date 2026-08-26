cd D:\editorBackend\tag_creator

$ROOT = (Get-Location).Path
$FOLDERS = @(
  "D:\Application\Website\ftpPrivate\Bowlnfub Kids Corner MP4",
  "D:\Application\Website\ftpPrivate\Bowlnfun Totems Vertical MP4",
  "D:\Application\Website\ftpPrivate\Bowlnfun Sing King Karaoke",
  "D:\Application\Website\ftpPrivate\Bowlnfun Escape Rooms MP4",
  "D:\Application\Website\ftpPrivate\Local Hero MP3",
  "D:\Application\Website\ftpPrivate\Bowlnfun Countdown Escape Rooms",
  "D:\Application\Website\ftpPrivate\Rock 2026",
  "D:\Application\Website\ftpPrivate\Alternative Rock 2026",
  "D:\Application\Website\ftpPrivate\LH MP3",
  "D:\Application\Website\ftpPrivate\XtendaMix",
  "D:\Application\Website\ftpPrivate\LH MP4",
  "D:\Application\Website\ftpPrivate\Bowlnfun Danish Karaoke"
)

function Run-Step {
  param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][scriptblock]$Command
  )

  Write-Host ""
  Write-Host "===== $Label =====" -ForegroundColor Cyan
  & $Command
  if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: $Label (exit=$LASTEXITCODE)" -ForegroundColor Red
    return $false
  }
  return $true
}

foreach ($INPUT_DIR in $FOLDERS) {
  if (!(Test-Path -LiteralPath $INPUT_DIR)) {
    Write-Host "SKIP missing folder: $INPUT_DIR" -ForegroundColor Yellow
    continue
  }

  $INPUT_NAME = Split-Path -Leaf $INPUT_DIR
  Write-Host ""
  Write-Host "############################################################" -ForegroundColor Green
  Write-Host "Processing: $INPUT_NAME" -ForegroundColor Green
  Write-Host "Source: $INPUT_DIR" -ForegroundColor Green
  Write-Host "############################################################" -ForegroundColor Green

  $mainOk = Run-Step "$INPUT_NAME - Main CSV" {
    docker run --rm --user root --cpus=4 --env-file .env `
      --env INPUT_DIR=/app/input_media `
      --env OUTPUT_DIR=/app/output `
      --env LOCAL_AI_MODELS_DIR=/app/models/local_ai `
      --env WORKER_THREADS=3 `
      --env TF_CPP_MIN_LOG_LEVEL=2 `
      --env CUDA_VISIBLE_DEVICES=-1 `
      --env MEDIA_NORMALIZATION_ENABLED=true `
      --env MEDIA_NORMALIZATION_TARGET_DB=89.0 `
      --env MEDIA_NORMALIZATION_DIR_NAME=normalization `
      --tmpfs /app/data --tmpfs /app/logs `
      -v "${ROOT}\output:/app/output" `
      -v "${INPUT_DIR}:/app/input_media" `
      -v "D:\editorBackend\tag_ai:/app/models/local_ai:ro" `
      tag_creator:local-ai `
      --input-dir /app/input_media `
      --report "/app/output/${INPUT_NAME}.csv" `
      --final-csv --no-debug-output --dry-run
  }
  if (!$mainOk) {
    continue
  }

  $changeOk = Run-Step "$INPUT_NAME - Change" {
    docker run --rm --user root --cpus=2 --memory=4g `
      --entrypoint python --env-file .env `
      --env CUDA_VISIBLE_DEVICES=-1 `
      --env TF_CPP_MIN_LOG_LEVEL=2 `
      --env HF_HUB_OFFLINE=1 `
      -v "${ROOT}\change.py:/app/change.py:ro" `
      -v "${ROOT}\tag_creator:/app/tag_creator:ro" `
      -v "${ROOT}\output:/app/output" `
      -v "${INPUT_DIR}:/app/input_media:ro" `
      -v "D:\editorBackend\tag_ai:/app/models/local_ai:ro" `
      tag_creator:local-ai change.py `
      --input "/app/output/${INPUT_NAME}.csv" `
      --media-root /app/input_media `
      --excel-time-text `
      --overwrite
  }
  if (!$changeOk) {
    continue
  }

  Run-Step "$INPUT_NAME - Split" {
    docker run --rm --user root `
      --entrypoint python `
      -v "${ROOT}\split.py:/app/split.py:ro" `
      -v "${ROOT}\output:/app/output" `
      -v "${ROOT}\clean:/app/clean" `
      -v "${INPUT_DIR}:/app/input_media:ro" `
      tag_creator:local-ai split.py `
      --input "/app/output/${INPUT_NAME}.csv" `
      --with-tag "/app/output/${INPUT_NAME}_with_tag.xlsx" `
      --output-dir /app/clean `
      --media-root /app/input_media `
      --overwrite
  } | Out-Null
}

Write-Host ""
Write-Host "All folder pipeline finished." -ForegroundColor Green
