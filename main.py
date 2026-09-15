#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolve_host_path(raw: str, default: str) -> Path:
    value = raw or default
    path = Path(value)
    if path.is_absolute():
        return path
    return (ROOT / path).resolve()


def _docker_path(path: Path) -> str:
    # Docker Desktop accepts Windows paths most reliably with forward slashes.
    return str(path.resolve()).replace("\\", "/")


def _run(command: list[str]) -> int:
    print("Running:")
    print(" ".join(f'"{item}"' if " " in item else item for item in command))
    return subprocess.call(command, cwd=ROOT)


def _image_exists(image: str) -> bool:
    return subprocess.call(
        ["docker", "image", "inspect", image],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) == 0


def _split_env_paths(raw: str) -> list[str]:
    return [value.strip().strip('"').strip("'") for value in raw.split(";") if value.strip()]


def _configured_input_dirs(env: dict[str, str]) -> list[Path]:
    numbered_values = [
        value
        for key, value in sorted(env.items())
        if key.startswith("HOST_INPUT_DIR_") and value.strip()
    ]
    if numbered_values:
        return [_resolve_host_path(value, "") for value in numbered_values]
    multi_value = env.get("HOST_INPUT_DIRS", "").strip()
    if multi_value:
        return [_resolve_host_path(value, "") for value in _split_env_paths(multi_value)]
    single_value = env.get("HOST_INPUT_DIR", "") or env.get("INPUT_DIR", "")
    return [_resolve_host_path(single_value, "../ftp_downloads/mp3")]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the full tag_creator Docker pipeline.")
    parser.add_argument("--build", action="store_true", help="build/rebuild the Docker image before running")
    parser.add_argument("--image", default=os.environ.get("TAG_CREATOR_IMAGE", "tag_creator:local-ai"))
    parser.add_argument("--limit", type=int, help="optional test limit; blank means all files")
    parser.add_argument("--workers", type=int, help="override WORKER_THREADS for this run")
    parser.add_argument("--cpus", default=None, help="Docker CPU quota, for example 2")
    parser.add_argument("--keep-cache", action="store_true", help="persist data/log CSV cache instead of tmpfs")
    parser.add_argument("--keep-output-history", action="store_true", help="do not remove old output CSV/JSON files before this run")
    parser.add_argument("--fresh", action="store_true", help="replace the current input folder CSV instead of resuming it")
    parser.add_argument("--main-only", action="store_true", help="only create the main CSV; skip change.py and split.py")
    return parser


def _run_pipeline_for_input(
    *,
    args: argparse.Namespace,
    env: dict[str, str],
    host_input: Path,
    output_dir: Path,
    clean_dir: Path,
    local_ai_host: Path,
) -> int:
    if not host_input.exists() or not host_input.is_dir():
        print(f"SKIP missing input folder: {host_input}", file=sys.stderr)
        return 0

    final_csv = output_dir / f"{host_input.name}.csv"
    final_name = final_csv.name
    with_tag_path = output_dir / f"{final_csv.stem}_with_tag.xlsx"
    if args.fresh:
        for stale in (final_csv, final_csv.with_suffix(".jsonl"), output_dir / "run_summary.json", with_tag_path):
            if stale.exists():
                stale.unlink()

    print("")
    print("############################################################")
    print(f"Processing: {host_input.name}")
    print(f"Source: {host_input}")
    print("############################################################")

    cpus = args.cpus or env.get("TAG_CREATOR_DOCKER_CPUS", "4")
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "root",
        f"--cpus={cpus}",
        "--env-file",
        ".env",
        "--env",
        "INPUT_DIR=/app/input_media",
        "--env",
        "OUTPUT_DIR=/app/output",
        "--env",
        "LOCAL_AI_MODELS_DIR=/app/models/local_ai",
    ]
    if not args.keep_cache:
        command.extend(["--tmpfs", "/app/data", "--tmpfs", "/app/logs"])
    else:
        data_dir = _resolve_host_path(env.get("DATA_DIR", ""), "data")
        log_dir = _resolve_host_path(env.get("LOG_DIR", ""), "logs")
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        command.extend(
            [
                "-v",
                f"{_docker_path(data_dir)}:/app/data",
                "-v",
                f"{_docker_path(log_dir)}:/app/logs",
            ]
        )

    command.extend(
        [
            "-v",
            f"{_docker_path(host_input)}:/app/input_media",
            "-v",
            f"{_docker_path(output_dir)}:/app/output",
            "-v",
            f"{_docker_path(local_ai_host)}:/app/models/local_ai:ro",
            "-v",
            f"{_docker_path(ROOT / 'tag_creator')}:/app/tag_creator:ro",
            args.image,
            "--input-dir",
            "/app/input_media",
            "--report",
            f"/app/output/{final_name}",
            "--final-csv",
            "--no-debug-output",
            "--dry-run",
        ]
    )
    if args.fresh:
        command.append("--no-resume")
    if args.limit is not None:
        command.extend(["--limit", str(args.limit)])
    if args.workers is not None:
        command.extend(["--workers", str(args.workers)])

    code = _run(command)
    if code != 0 or args.main_only:
        if code == 0:
            print(f"Final enriched CSV: {final_csv}")
        return code

    change_command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "root",
        "--cpus=2",
        "--memory=4g",
        "--entrypoint",
        "python",
        "--env-file",
        ".env",
        "--env",
        "CUDA_VISIBLE_DEVICES=-1",
        "--env",
        "TF_CPP_MIN_LOG_LEVEL=2",
        "--env",
        "HF_HUB_OFFLINE=1",
        "-v",
        f"{_docker_path(ROOT / 'change.py')}:/app/change.py:ro",
        "-v",
        f"{_docker_path(ROOT / 'tag_creator')}:/app/tag_creator:ro",
        "-v",
        f"{_docker_path(output_dir)}:/app/output",
        "-v",
        f"{_docker_path(host_input)}:/app/input_media:ro",
        "-v",
        f"{_docker_path(local_ai_host)}:/app/models/local_ai:ro",
        args.image,
        "change.py",
        "--input",
        f"/app/output/{final_name}",
        "--media-root",
        "/app/input_media",
        "--excel-time-text",
        "--overwrite",
    ]
    code = _run(change_command)
    if code != 0:
        return code

    split_command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "root",
        "--entrypoint",
        "python",
        "-v",
        f"{_docker_path(ROOT / 'split.py')}:/app/split.py:ro",
        "-v",
        f"{_docker_path(ROOT / 'tag_creator')}:/app/tag_creator:ro",
        "-v",
        f"{_docker_path(output_dir)}:/app/output",
        "-v",
        f"{_docker_path(clean_dir)}:/app/clean",
        "-v",
        f"{_docker_path(host_input)}:/app/input_media:ro",
        args.image,
        "split.py",
        "--input",
        f"/app/output/{final_name}",
        "--with-tag",
        f"/app/output/{with_tag_path.name}",
        "--output-dir",
        "/app/clean",
        "--media-root",
        "/app/input_media",
        "--overwrite",
    ]
    code = _run(split_command)
    if code != 0:
        return code

    print(f"Main CSV: {final_csv}")
    print(f"With-tag XLSX: {with_tag_path}")
    print(f"Clean split output: {clean_dir / final_csv.stem}")
    print(f"MP3 normalization folders: {host_input}\\...\\normalization-mp3")
    print(f"MP4 normalization folders: {host_input}\\...\\normalization-mp4")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    env_path = ROOT / ".env"
    env = _read_env(env_path)
    if not env_path.exists():
        print("Missing .env. Copy .env.example to .env and fill INPUT_DIR/API keys first.", file=sys.stderr)
        return 2

    input_dirs = _configured_input_dirs(env)
    if not input_dirs:
        print("No input folders configured. Set HOST_INPUT_DIRS or HOST_INPUT_DIR in .env.", file=sys.stderr)
        return 2
    output_dir = _resolve_host_path(env.get("OUTPUT_DIR", ""), "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_dir = _resolve_host_path(env.get("CLEAN_OUTPUT_DIR", "") or env.get("CLEAN_DIR", ""), "clean")
    clean_dir.mkdir(parents=True, exist_ok=True)
    local_ai_host = _resolve_host_path(env.get("LOCAL_AI_HOST_DIR", ""), "models/local_ai")
    if not local_ai_host.exists():
        print(f"Warning: local AI model folder not found yet: {local_ai_host}")
        print("The run will still use free APIs/web/rules, but local AI providers may self-skip.")

    if args.build or not _image_exists(args.image):
        build = ["docker", "build", "--build-arg", "INSTALL_LOCAL_AI=true", "-t", args.image, "."]
        code = _run(build)
        if code != 0:
            return code

    failed = 0
    for host_input in input_dirs:
        code = _run_pipeline_for_input(
            args=args,
            env=env,
            host_input=host_input,
            output_dir=output_dir,
            clean_dir=clean_dir,
            local_ai_host=local_ai_host,
        )
        if code != 0:
            failed += 1
            if args.main_only:
                return code

    if failed:
        print(f"Pipeline finished with failures: {failed}/{len(input_dirs)}")
        return 1
    print(f"Pipeline finished successfully for {len(input_dirs)} folder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
