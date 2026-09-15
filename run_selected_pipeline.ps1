param(
  [switch]$Fresh,
  [switch]$MainOnly,
  [string]$Image = "tag_creator:local-ai",
  [string]$Cpus = ""
)

$ErrorActionPreference = "Stop"
$ROOT = (Get-Location).Path
$EnvFile = Join-Path $ROOT ".env"

if (!(Test-Path -LiteralPath $EnvFile)) {
  Write-Host "Missing .env file: $EnvFile" -ForegroundColor Red
  exit 2
}

function Read-DotEnv {
  param([string]$Path)
  $Values = @{}
  Get-Content -LiteralPath $Path | ForEach-Object {
    $Line = $_.Trim()
    if (!$Line -or $Line.StartsWith("#") -or !$Line.Contains("=")) {
      return
    }
    $Parts = $Line.Split("=", 2)
    $Key = $Parts[0].Trim()
    $Value = $Parts[1].Trim().Trim('"').Trim("'")
    $Values[$Key] = $Value
  }
  return $Values
}

function Get-EnvValue {
  param(
    [hashtable]$EnvValues,
    [string]$Key,
    [string]$Default = ""
  )
  if ($EnvValues.ContainsKey($Key) -and $EnvValues[$Key]) {
    return $EnvValues[$Key]
  }
  return $Default
}

function Resolve-HostPath {
  param(
    [string]$Value,
    [string]$Default = ""
  )
  $Raw = if ($Value) { $Value } else { $Default }
  if (!$Raw) {
    return ""
  }
  $Expanded = [Environment]::ExpandEnvironmentVariables($Raw)
  if ([System.IO.Path]::IsPathRooted($Expanded)) {
    return $Expanded
  }
  return (Resolve-Path -LiteralPath (Join-Path $ROOT $Expanded)).Path
}

function Get-InputFolders {
  param([hashtable]$EnvValues)
  $Folders = New-Object System.Collections.Generic.List[string]

  $NumberedKeys = $EnvValues.Keys |
    Where-Object { $_ -match '^HOST_INPUT_DIR_\d+$' -and $EnvValues[$_] } |
    Sort-Object
  foreach ($Key in $NumberedKeys) {
    $Folders.Add((Resolve-HostPath $EnvValues[$Key]))
  }

  if ($Folders.Count -gt 0) {
    return $Folders
  }

  $Joined = Get-EnvValue $EnvValues "HOST_INPUT_DIRS"
  if ($Joined) {
    foreach ($Item in $Joined.Split(";")) {
      $Clean = $Item.Trim()
      if ($Clean) {
        $Folders.Add((Resolve-HostPath $Clean))
      }
    }
    return $Folders
  }

  $Single = Get-EnvValue $EnvValues "HOST_INPUT_DIR"
  if (!$Single) {
    $Single = Get-EnvValue $EnvValues "INPUT_DIR"
  }
  if ($Single) {
    $Folders.Add((Resolve-HostPath $Single))
  }
  return $Folders
}

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

$EnvValues = Read-DotEnv $EnvFile
$OutputDir = Resolve-HostPath (Get-EnvValue $EnvValues "OUTPUT_DIR" "output")
$CleanDir = Resolve-HostPath (Get-EnvValue $EnvValues "CLEAN_OUTPUT_DIR" (Get-EnvValue $EnvValues "CLEAN_DIR" "clean"))
$LocalAiDir = Resolve-HostPath (Get-EnvValue $EnvValues "LOCAL_AI_HOST_DIR" "D:/editorBackend/tag_ai")
$DockerCpus = if ($Cpus) { $Cpus } else { Get-EnvValue $EnvValues "TAG_CREATOR_DOCKER_CPUS" "4" }
$InputFolders = Get-InputFolders $EnvValues

if ($InputFolders.Count -eq 0) {
  Write-Host "No input folders configured. Set HOST_INPUT_DIR_01, HOST_INPUT_DIRS, or HOST_INPUT_DIR in .env." -ForegroundColor Red
  exit 2
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $CleanDir | Out-Null

$Failed = 0
foreach ($INPUT_DIR in $InputFolders) {
  if (!(Test-Path -LiteralPath $INPUT_DIR)) {
    Write-Host "SKIP missing folder: $INPUT_DIR" -ForegroundColor Yellow
    continue
  }

  $INPUT_NAME = Split-Path -Leaf $INPUT_DIR
  $MainCsv = Join-Path $OutputDir "$INPUT_NAME.csv"
  $WithTag = Join-Path $OutputDir "$($INPUT_NAME)_with_tag.xlsx"

  if ($Fresh) {
    Remove-Item -LiteralPath $MainCsv -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ([System.IO.Path]::ChangeExtension($MainCsv, ".jsonl")) -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $WithTag -Force -ErrorAction SilentlyContinue
  }

  Write-Host ""
  Write-Host "############################################################" -ForegroundColor Green
  Write-Host "Processing: $INPUT_NAME" -ForegroundColor Green
  Write-Host "Source: $INPUT_DIR" -ForegroundColor Green
  Write-Host "############################################################" -ForegroundColor Green

  $mainOk = Run-Step "$INPUT_NAME - Main CSV + normalization" {
    $Args = @(
      "run", "--rm", "--user", "root", "--cpus=$DockerCpus", "--env-file", ".env",
      "--env", "INPUT_DIR=/app/input_media",
      "--env", "OUTPUT_DIR=/app/output",
      "--env", "LOCAL_AI_MODELS_DIR=/app/models/local_ai",
      "--tmpfs", "/app/data", "--tmpfs", "/app/logs",
      "-v", "${INPUT_DIR}:/app/input_media",
      "-v", "${OutputDir}:/app/output",
      "-v", "${LocalAiDir}:/app/models/local_ai:ro",
      "-v", "${ROOT}\tag_creator:/app/tag_creator:ro",
      $Image,
      "--input-dir", "/app/input_media",
      "--report", "/app/output/$INPUT_NAME.csv",
      "--final-csv", "--no-debug-output", "--dry-run"
    )
    if ($Fresh) {
      $Args += "--no-resume"
    }
    docker @Args
  }
  if (!$mainOk) {
    $Failed += 1
    continue
  }
  if ($MainOnly) {
    continue
  }

  $changeOk = Run-Step "$INPUT_NAME - Change with artist repair" {
    docker run --rm --user root --cpus=2 --memory=4g `
      --entrypoint python --env-file .env `
      --env CUDA_VISIBLE_DEVICES=-1 `
      --env TF_CPP_MIN_LOG_LEVEL=2 `
      --env HF_HUB_OFFLINE=1 `
      -v "${ROOT}\change.py:/app/change.py:ro" `
      -v "${ROOT}\tag_creator:/app/tag_creator:ro" `
      -v "${OutputDir}:/app/output" `
      -v "${INPUT_DIR}:/app/input_media:ro" `
      -v "${LocalAiDir}:/app/models/local_ai:ro" `
      $Image change.py `
      --input "/app/output/$INPUT_NAME.csv" `
      --media-root /app/input_media `
      --excel-time-text `
      --overwrite
  }
  if (!$changeOk) {
    $Failed += 1
    continue
  }

  $splitOk = Run-Step "$INPUT_NAME - Split final clean output" {
    docker run --rm --user root `
      --entrypoint python `
      -v "${ROOT}\split.py:/app/split.py:ro" `
      -v "${ROOT}\tag_creator:/app/tag_creator:ro" `
      -v "${OutputDir}:/app/output" `
      -v "${CleanDir}:/app/clean" `
      -v "${INPUT_DIR}:/app/input_media:ro" `
      $Image split.py `
      --input "/app/output/$INPUT_NAME.csv" `
      --with-tag "/app/output/$($INPUT_NAME)_with_tag.xlsx" `
      --output-dir /app/clean `
      --media-root /app/input_media `
      --overwrite
  }
  if (!$splitOk) {
    $Failed += 1
    continue
  }

  Write-Host "Main CSV: $MainCsv" -ForegroundColor Green
  Write-Host "With-tag XLSX: $WithTag" -ForegroundColor Green
  Write-Host "Clean output: $(Join-Path $CleanDir $INPUT_NAME)" -ForegroundColor Green
}

if ($Failed -gt 0) {
  Write-Host "Pipeline finished with failures: $Failed" -ForegroundColor Red
  exit 1
}

Write-Host ""
Write-Host "Pipeline finished successfully." -ForegroundColor Green
