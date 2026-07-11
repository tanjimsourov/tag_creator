#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_creator.config import load_settings


def main() -> int:
    settings = load_settings()
    endpoint = settings.sonoteller_analyze_endpoint
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    file_url = settings.sonoteller_file_url_base or "https://your-server.example.com/audio"
    sample_file = f"{file_url.rstrip('/')}/sample-song.mp3"
    print("SONOTELLER / RapidAPI sample call")
    print()
    print("curl --request POST \\")
    print(f'  --url "{settings.sonoteller_base_url}{endpoint}" \\')
    print('  --header "Content-Type: application/json" \\')
    print('  --header "X-RapidAPI-Key: YOUR_RAPIDAPI_KEY" \\')
    print(f'  --header "X-RapidAPI-Host: {settings.sonoteller_rapidapi_host}" \\')
    print(f'  --data "{{\\"file\\":\\"{sample_file}\\"}}"')
    print()
    print("Required .env values before live use:")
    print("SONOTELLER_RAPIDAPI_KEY=<RapidAPI key>")
    print(f"SONOTELLER_RAPIDAPI_HOST={settings.sonoteller_rapidapi_host}")
    print(f"SONOTELLER_BASE_URL={settings.sonoteller_base_url}")
    print(f"SONOTELLER_ANALYZE_ENDPOINT={settings.sonoteller_analyze_endpoint}")
    print("SONOTELLER_FILE_URL_BASE=<public URL where owned audio files are reachable>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
