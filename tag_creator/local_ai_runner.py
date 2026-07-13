from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

for _thread_var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "TF_NUM_INTRAOP_THREADS",
    "TF_NUM_INTEROP_THREADS",
):
    os.environ.setdefault(_thread_var, "2")


def _load_labels(path: Path) -> list[str]:
    labels: list[str] = []
    with path.open(encoding="utf-8") as labels_file:
        for line in labels_file:
            label = line.strip()
            if label:
                labels.append(label)
    return labels


def _top_tags(predictions: Any, labels: list[str], top_n: int) -> list[dict[str, Any]]:
    import numpy as np

    array = np.asarray(predictions)
    if array.ndim > 1:
        array = array.mean(axis=0)
    array = array.reshape(-1)
    ranked = np.argsort(array)[::-1][:top_n]
    tags: list[dict[str, Any]] = []
    for index in ranked:
        label = labels[int(index)] if int(index) < len(labels) else f"label_{int(index)}"
        tags.append({"label": label, "score": round(float(array[int(index)]), 6)})
    return tags


def _load_audio(audio_path: Path, rate: int = 16000):
    from essentia.standard import MonoLoader

    return MonoLoader(filename=str(audio_path), sampleRate=rate, resampleQuality=4)()


def run_essentia_features(args: argparse.Namespace) -> dict[str, Any]:
    """Algorithmic (non-ML) descriptors: BPM, musical key/scale, danceability.

    Needs only Essentia — no downloaded model files — and fills exactly the
    fields (bpm/key/danceability) that otherwise push a track to the paid stage.
    Each extractor degrades independently so one failure never blanks the rest.
    """
    from essentia.standard import Danceability, KeyExtractor, RhythmExtractor2013

    audio = _load_audio(args.audio, rate=44100)
    features: dict[str, Any] = {}
    try:
        bpm, _beats, beats_confidence, _estimates, _intervals = RhythmExtractor2013(method="multifeature")(audio)
        features["bpm"] = round(float(bpm))
        features["bpm_confidence"] = round(float(beats_confidence), 3)
    except Exception as exc:  # noqa: BLE001
        features["bpm_error"] = str(exc)
    try:
        key, scale, strength = KeyExtractor()(audio)
        features["key"] = key
        features["scale"] = scale
        features["key_strength"] = round(float(strength), 3)
    except Exception as exc:  # noqa: BLE001
        features["key_error"] = str(exc)
    try:
        danceability, _dfa = Danceability()(audio)
        features["danceability"] = round(float(danceability), 3)
    except Exception as exc:  # noqa: BLE001
        features["danceability_error"] = str(exc)
    return {"provider": "essentia_features", "audio": str(args.audio), "features": features}


def _run_prediction_head(embeddings: Any, model_path: str, output_node: str = "") -> Any:
    from essentia.standard import TensorflowPredict2D

    if output_node:
        return TensorflowPredict2D(graphFilename=model_path, output=output_node)(embeddings)
    return TensorflowPredict2D(graphFilename=model_path)(embeddings)


def run_essentia_discogs_effnet(args: argparse.Namespace) -> dict[str, Any]:
    from essentia.standard import TensorflowPredictEffnetDiscogs

    # Compute the shared Discogs-EffNet embedding ONCE, then run every prediction
    # head off it (genre + any extra mood/theme/instrument heads).
    audio = _load_audio(args.audio, rate=16000)
    embeddings = TensorflowPredictEffnetDiscogs(graphFilename=str(args.embedding_model))(audio)

    tags: list[dict[str, Any]] = []
    base_predictions = _run_prediction_head(embeddings, str(args.prediction_model))
    for tag in _top_tags(base_predictions, _load_labels(args.labels), args.top_n):
        tag["head"] = "genre"
        tags.append(tag)

    for spec in args.head or []:
        parts = spec.split("|")
        model_path = parts[0].strip()
        labels_path = parts[1].strip() if len(parts) > 1 else ""
        output_node = parts[2].strip() if len(parts) > 2 else ""
        head_name = Path(model_path).stem
        try:
            predictions = _run_prediction_head(embeddings, model_path, output_node)
            head_labels = _load_labels(Path(labels_path)) if labels_path else []
            for tag in _top_tags(predictions, head_labels, args.top_n):
                tag["head"] = head_name
                tags.append(tag)
        except Exception as exc:  # noqa: BLE001 - a broken head must not kill the rest
            print(f"prediction head failed ({model_path}): {exc}", file=sys.stderr)
            continue

    return {
        "provider": "essentia_discogs_effnet",
        "audio": str(args.audio),
        "embedding_model": str(args.embedding_model),
        "prediction_model": str(args.prediction_model),
        "labels": str(args.labels),
        "tags": tags,
    }


def run_musicnn_mtg_jamendo(args: argparse.Namespace) -> dict[str, Any]:
    from essentia.standard import TensorflowPredictMusiCNN

    audio = _load_audio(args.audio, rate=16000)
    predictions = TensorflowPredictMusiCNN(graphFilename=str(args.prediction_model))(audio)
    labels = _load_labels(args.labels)
    tags = _top_tags(predictions, labels, args.top_n)
    # Flat autotagger: label vocabulary mixes genre/mood/instrument/vocals, so the
    # mapper classifies by label, not head. Mark the source for provenance.
    for tag in tags:
        tag.setdefault("head", "msd_autotag")
    return {
        "provider": "musicnn_mtg_jamendo",
        "audio": str(args.audio),
        "prediction_model": str(args.prediction_model),
        "labels": str(args.labels),
        "tags": tags,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run optional local AI music tagging models.")
    subparsers = parser.add_subparsers(dest="provider", required=True)

    features = subparsers.add_parser("essentia_features")
    features.add_argument("--audio", type=Path, required=True)

    discogs = subparsers.add_parser("essentia_discogs_effnet")
    discogs.add_argument("--audio", type=Path, required=True)
    discogs.add_argument("--embedding-model", type=Path, required=True)
    discogs.add_argument("--prediction-model", type=Path, required=True)
    discogs.add_argument("--labels", type=Path, required=True)
    discogs.add_argument("--top-n", type=int, default=12)
    discogs.add_argument(
        "--head",
        action="append",
        default=[],
        help="extra prediction head sharing the embedding: 'model.pb|labels.txt' or 'model.pb|labels.txt|output_node'",
    )

    musicnn = subparsers.add_parser("musicnn_mtg_jamendo")
    musicnn.add_argument("--audio", type=Path, required=True)
    musicnn.add_argument("--prediction-model", type=Path, required=True)
    musicnn.add_argument("--labels", type=Path, required=True)
    musicnn.add_argument("--top-n", type=int, default=12)
    return parser


def _validate_paths(args: argparse.Namespace) -> None:
    for name in ("audio", "embedding_model", "prediction_model", "labels"):
        path = getattr(args, name, None)
        if path is not None and not path.exists():
            raise FileNotFoundError(f"{name.replace('_', '-')} not found: {path}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _validate_paths(args)
        if args.provider == "essentia_features":
            result = run_essentia_features(args)
        elif args.provider == "essentia_discogs_effnet":
            result = run_essentia_discogs_effnet(args)
        elif args.provider == "musicnn_mtg_jamendo":
            result = run_musicnn_mtg_jamendo(args)
        else:
            raise ValueError(f"unsupported provider: {args.provider}")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
