"""Local semantic embeddings via fastembed (ONNX, CPU-only).

Everything degrades gracefully: if fastembed or its model is unavailable, the
embedder reports ``enabled == False`` and the store falls back to lexical
retrieval. Writes never fail because of embeddings.
"""

from __future__ import annotations

import threading
import warnings
from typing import Any, Sequence

import numpy as np


class Embedder:
    def __init__(self, model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                 enabled: bool = True, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._enabled = bool(enabled)
        self._model: Any = None
        self._lock = threading.Lock()
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._failed

    def _ensure(self) -> None:
        if not self._enabled or self._model is not None or self._failed:
            return
        with self._lock:
            if self._model is not None or self._failed:
                return
            try:
                from fastembed import TextEmbedding

                kwargs: dict[str, Any] = {"model_name": self.model_name}
                if self.cache_dir:
                    kwargs["cache_dir"] = self.cache_dir
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self._model = TextEmbedding(**kwargs)
            except Exception:
                self._failed = True

    def embed(self, texts: Sequence[str]) -> Any:
        if not self.enabled or not texts:
            return None
        self._ensure()
        if self._model is None:
            return None
        try:
            vectors = list(self._model.embed(list(texts)))
            return np.asarray(vectors, dtype=np.float32)
        except Exception:
            return None


def pack(vector: Any) -> bytes:
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    return arr.tobytes()


def unpack(blob: bytes, dim: int) -> Any:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


def cosines(query_vector: Any, matrix: Any) -> Any:
    q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
    m = np.asarray(matrix, dtype=np.float32)
    denom = np.linalg.norm(m, axis=1) * np.linalg.norm(q)
    denom[denom == 0] = 1e-9
    return (m @ q) / denom
