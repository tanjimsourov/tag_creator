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


def _split_label_spec(spec: str) -> tuple[str, str]:
    if ":" not in spec:
        return "theme", spec.strip()
    field, value = spec.split(":", 1)
    return field.strip().lower(), value.strip()


def _clap_prompts(field: str, value: str) -> list[str]:
    """Prompt ensemble for stronger zero-shot matching.

    CLAP is sensitive to wording. A small field-specific prompt ensemble gives a
    more stable score than one generic sentence, especially for business tags
    such as retail occasion, energy, season and age group.
    """
    field = field.lower().strip()
    value = value.strip()
    if field == "genre":
        return [
            f"a {value} music track",
            f"this song belongs to the {value} genre",
            f"music with a {value} sound",
        ]
    if field == "subgenre":
        return [
            f"a {value} music track",
            f"this song has a {value} style",
            f"music with {value} production",
        ]
    if field in {"mood", "moods"}:
        return [
            f"music that feels {value}",
            f"a song with a {value} mood",
            f"audio with a {value} emotional tone",
        ]
    if field == "energy":
        return [
            f"a song with {value} energy",
            f"music that sounds {value} energy",
            f"a track with {value} intensity",
        ]
    if field == "valence":
        return [
            f"music with a {value} emotional feeling",
            f"a song that sounds {value}",
            f"audio with {value} valence",
        ]
    if field == "danceability":
        return [
            f"a {value} danceable music track",
            f"music suitable for dancing at a {value} level",
            f"a song with {value} rhythm movement",
        ]
    if field in {"instrument", "instruments"}:
        return [
            f"music featuring {value}",
            f"a song with {value} instrumentation",
            f"audio where {value} is present",
        ]
    if field == "vocals":
        return [
            f"a {value} music track",
            f"music with {value}",
            f"audio that is {value}",
        ]
    if field == "occasion":
        return [
            f"music suitable for {value}",
            f"a track for {value}",
            f"background music for {value}",
        ]
    if field == "weather":
        return [
            f"music suitable for {value} weather",
            f"a song for a {value} day",
            f"background music matching {value} weather",
        ]
    if field == "season":
        return [
            f"music suitable for {value}",
            f"a song with a {value} feeling",
            f"background music for {value}",
        ]
    if field == "age_group":
        return [
            f"music suitable for a {value} audience",
            f"a song for {value} listeners",
            f"retail music aimed at {value}",
        ]
    if field == "language":
        return [
            f"a song with {value} vocals",
            f"music sung in {value}",
            f"lyrics language is {value}",
        ]
    return [
        f"a music track with {field} {value}",
        f"music where {field} is {value}",
    ]


def run_clap_zero_shot(args: argparse.Namespace) -> dict[str, Any]:
    """Zero-shot audio tagging with LAION CLAP.

    CLAP is useful for broad descriptive tags (genre/mood/theme/instrument). It
    is intentionally not used for factual catalog fields like artist/title/year.
    Model files are cached in the mounted local-AI directory.
    """
    import numpy as np
    import torch
    from transformers import ClapModel, ClapProcessor

    audio = _load_audio(args.audio, rate=48000)
    # Very long files are expensive and unnecessary for descriptors. Analyze the
    # first N seconds consistently so batch timing stays bounded.
    max_samples = int(48000 * args.max_seconds)
    if len(audio) > max_samples:
        audio = audio[:max_samples]
    audio = np.asarray(audio, dtype=np.float32)

    label_specs = [spec for spec in args.label if spec.strip()]
    if not label_specs:
        raise ValueError("at least one --label is required")
    fields_values = [_split_label_spec(spec) for spec in label_specs]
    prompt_specs: list[tuple[int, str, str, str]] = []
    prompts: list[str] = []
    for spec_index, (field, value) in enumerate(fields_values):
        for prompt in _clap_prompts(field, value):
            prompt_specs.append((spec_index, field, value, prompt))
            prompts.append(prompt)

    processor = ClapProcessor.from_pretrained(args.model_name, cache_dir=str(args.cache_dir))
    model = ClapModel.from_pretrained(args.model_name, cache_dir=str(args.cache_dir))
    model.eval()
    inputs = processor(text=prompts, audios=audio, sampling_rate=48000, return_tensors="pt", padding=True)
    with torch.no_grad():
        outputs = model(**inputs)
        scores = outputs.logits_per_audio.softmax(dim=-1).cpu().numpy()[0]

    # Aggregate prompt variants back to the original label spec. Max keeps a
    # strong exact wording from being diluted by weaker variants.
    aggregated: dict[int, dict[str, Any]] = {}
    for prompt_index, score in enumerate(scores):
        spec_index, field, value, prompt = prompt_specs[prompt_index]
        entry = aggregated.setdefault(
            spec_index,
            {"field": field, "label": value, "score": 0.0, "best_prompt": prompt, "prompt_count": 0},
        )
        entry["prompt_count"] += 1
        if float(score) > float(entry["score"]):
            entry["score"] = float(score)
            entry["best_prompt"] = prompt

    ranked = sorted(aggregated.values(), key=lambda item: float(item["score"]), reverse=True)[: args.top_n]
    tags: list[dict[str, Any]] = []
    for item in ranked:
        tags.append(
            {
                "field": item["field"],
                "label": item["label"],
                "score": round(float(item["score"]), 6),
                "head": "clap_zero_shot",
                "prompt": item["best_prompt"],
                "prompt_count": item["prompt_count"],
            }
        )
    return {
        "provider": "clap_zero_shot",
        "audio": str(args.audio),
        "model": args.model_name,
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

    clap = subparsers.add_parser("clap_zero_shot")
    clap.add_argument("--audio", type=Path, required=True)
    clap.add_argument("--model-name", required=True)
    clap.add_argument("--cache-dir", type=Path, required=True)
    clap.add_argument("--label", action="append", default=[])
    clap.add_argument("--top-n", type=int, default=12)
    clap.add_argument("--max-seconds", type=int, default=45)
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
        elif args.provider == "clap_zero_shot":
            result = run_clap_zero_shot(args)
        else:
            raise ValueError(f"unsupported provider: {args.provider}")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
