"""Local, revision-locked embedding providers."""

from __future__ import annotations

import json
import os
import platform
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastembed import TextEmbedding
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

from supermind_memory.schema import EMBEDDING_DIMENSION


class EmbeddingProvider(Protocol):
    """Embedding boundary used by indexing and retrieval."""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class ModelLock:
    name: str
    revision: str
    dimension: int


def _load_model_lock() -> ModelLock:
    from supermind_memory.resources import distribution_data
    lock_path = distribution_data("model.lock.json")
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    return ModelLock(
        name=payload["model"],
        revision=payload["revision"],
        dimension=payload["dimension"],
    )


class FastEmbedProvider:
    """FastEmbed opened from an immutable Hugging Face snapshot."""

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        model_lock = _load_model_lock()
        if model_name != model_lock.name:
            raise ValueError(f"model {model_name!r} is not the locked embedding model")
        if model_lock.dimension != EMBEDDING_DIMENSION:
            raise ValueError("locked model dimension does not match the storage schema")

        cache_dir.mkdir(parents=True, exist_ok=True)
        download_kwargs = {
            "repo_id": model_lock.name,
            "revision": model_lock.revision,
            "cache_dir": str(cache_dir),
            "allow_patterns": [
                "config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
                _locked_onnx_filename(),
            ],
        }
        try:
            snapshot = snapshot_download(**download_kwargs, local_files_only=True)
        except LocalEntryNotFoundError:
            if _offline_enabled():
                raise
            snapshot = snapshot_download(**download_kwargs)

        self.snapshot_dir = Path(snapshot).resolve()
        model_dir = _fastembed_layout(self.snapshot_dir, cache_dir, model_lock.revision)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"The model .* now uses mean pooling instead of CLS embedding\..*",
                category=UserWarning,
            )
            self._model = TextEmbedding(
                model_name=model_lock.name,
                cache_dir=str(cache_dir),
                specific_model_path=str(model_dir),
                local_files_only=True,
            )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return _materialize(self._model.embed(list(texts)))

    def embed_query(self, text: str) -> list[float]:
        return _materialize(self._model.query_embed(text))[0]


def _fastembed_layout(snapshot_dir: Path, cache_dir: Path, revision: str) -> Path:
    """Adapt the upstream snapshot layout to FastEmbed 0.8's expected filenames."""
    if (snapshot_dir / "model_optimized.onnx").is_file():
        return snapshot_dir

    onnx_candidates = (
        snapshot_dir / _locked_onnx_filename(),
        snapshot_dir / "onnx" / "model.onnx",
        snapshot_dir / "onnx" / "model_O2.onnx",
        snapshot_dir / "onnx" / "model_O3.onnx",
    )
    onnx_model = next((path for path in onnx_candidates if path.is_file()), None)
    if onnx_model is None:
        raise FileNotFoundError("locked model snapshot contains no FastEmbed-compatible ONNX model")

    layout = cache_dir / "fastembed-layout" / revision
    layout.mkdir(parents=True, exist_ok=True)
    required_files = ("config.json", "tokenizer.json", "tokenizer_config.json")
    optional_files = ("special_tokens_map.json", "added_tokens.json")
    for filename in required_files:
        source = snapshot_dir / filename
        if not source.is_file():
            raise FileNotFoundError(f"locked model snapshot is missing {filename}")
        _link(source, layout / filename)
    for filename in optional_files:
        source = snapshot_dir / filename
        if source.is_file():
            _link(source, layout / filename)
    _link(onnx_model, layout / "model_optimized.onnx")
    return layout


def _locked_onnx_filename() -> str:
    architecture = platform.machine().casefold()
    if architecture in {"arm64", "aarch64"}:
        return "onnx/model_qint8_arm64.onnx"
    return "onnx/model_quint8_avx2.onnx"


def _offline_enabled() -> bool:
    truthy = {"1", "ON", "TRUE", "YES"}
    return any(
        os.environ.get(name, "").strip().upper() in truthy
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    )


def _link(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.resolve() == source.resolve():
            return
        destination.unlink()
    destination.symlink_to(source.resolve())


def _materialize(vectors) -> list[list[float]]:
    result = [[float(value) for value in vector] for vector in vectors]
    if any(len(vector) != EMBEDDING_DIMENSION for vector in result):
        raise ValueError(f"embedding must contain {EMBEDDING_DIMENSION} values")
    return result
