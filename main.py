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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run tag_creator in Docker and write one final enriched CSV.")
    parser.add_argument("--build", action="store_true", help="build/rebuild the Docker image before running")
    parser.add_argument("--image", default=os.environ.get("TAG_CREATOR_IMAGE", "tag_creator:local-ai"))
    parser.add_argument("--limit", type=int, help="optional test limit; blank means all files")
    parser.add_argument("--workers", type=int, help="override WORKER_THREADS for this run")
    parser.add_argument("--cpus", default=None, help="Docker CPU quota, for example 2")
    parser.add_argument("--keep-cache", action="store_true", help="persist data/log CSV cache instead of tmpfs")
    parser.add_argument("--keep-output-history", action="store_true", help="do not remove old output CSV/JSON files before this run")
    parser.add_argument("--fresh", action="store_true", help="replace the current input folder CSV instead of resuming it")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_path = ROOT / ".env"
    env = _read_env(env_path)
    if not env_path.exists():
        print("Missing .env. Copy .env.example to .env and fill INPUT_DIR/API keys first.", file=sys.stderr)
        return 2

    host_input = _resolve_host_path(env.get("HOST_INPUT_DIR", "") or env.get("INPUT_DIR", ""), "../ftp_downloads/mp3")
    if not host_input.exists() or not host_input.is_dir():
        print(f"Input folder does not exist: {host_input}", file=sys.stderr)
        print("Update INPUT_DIR in .env to the folder containing MP3/MP4 files.", file=sys.stderr)
        return 2

    output_dir = _resolve_host_path(env.get("OUTPUT_DIR", ""), "output")
    output_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"{host_input.name}.csv"
    final_csv = output_dir / final_name
    final_name = final_csv.name
    # Keep the active CSV so normal runs resume from its existing rows. Remove
    # unrelated old artifacts unless history was explicitly requested.
    if not args.keep_output_history:
        for stale in output_dir.iterdir():
            if stale.is_file() and stale != final_csv and stale.suffix.lower() in {".csv", ".json", ".jsonl"}:
                stale.unlink()
    if args.fresh:
        for stale in (final_csv, final_csv.with_suffix(".jsonl"), output_dir / "run_summary.json"):
            if stale.exists():
                stale.unlink()

    local_ai_host = _resolve_host_path(env.get("LOCAL_AI_HOST_DIR", ""), "models/local_ai")
    if not local_ai_host.exists():
        print(f"Warning: local AI model folder not found yet: {local_ai_host}")
        print("The run will still use free APIs/web/rules, but local AI providers may self-skip.")

    if args.build or not _image_exists(args.image):
        build = ["docker", "build", "--build-arg", "INSTALL_LOCAL_AI=true", "-t", args.image, "."]
        code = _run(build)
        if code != 0:
            return code

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
            f"{_docker_path(host_input)}:/app/input_media:ro",
            "-v",
            f"{_docker_path(output_dir)}:/app/output",
            "-v",
            f"{_docker_path(local_ai_host)}:/app/models/local_ai:ro",
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
    if code == 0:
        print(f"Final enriched CSV: {final_csv}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
