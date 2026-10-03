"""Face-embedding store for the Phase 1 laptop harness.

Format on disk: ``<faces_dir>/<name>.npy``, one 128-d ``float64`` embedding per
person, saved by ``numpy.save``.

This module is the laptop prototype of ``shield_rpi/recognizer.py``. Phase 2
promotes that logic into the RPi package properly; see DECISIONS.md D-010.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

LOGGER = logging.getLogger(__name__)

EMBEDDING_DIMS = 128


class FaceStoreError(RuntimeError):
    """Raised when the on-disk face store is missing or malformed."""


def faces_dir(path: str | Path) -> Path:
    """Return ``path`` as a :class:`Path`, creating it if absent."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def names(directory: str | Path) -> list[str]:
    """Return enrolled person names, sorted for deterministic matching order."""
    return sorted(p.stem for p in Path(directory).glob("*.npy"))


def load_all(directory: str | Path) -> tuple[list[str], list[np.ndarray]]:
    """Load every ``*.npy`` embedding.

    Returns ``(names, embeddings)`` in matching order. Malformed files are logged
    and skipped rather than raising, so one bad file cannot take the whole
    recogniser down.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return [], []

    labels: list[str] = []
    vectors: list[np.ndarray] = []
    for path in sorted(directory.glob("*.npy")):
        try:
            vector = np.load(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("skipping unreadable embedding %s: %s", path.name, exc)
            continue
        if vector.shape != (EMBEDDING_DIMS,):
            LOGGER.warning(
                "skipping %s: expected shape (%d,), got %s", path.name, EMBEDDING_DIMS, vector.shape
            )
            continue
        labels.append(path.stem)
        vectors.append(np.asarray(vector, dtype=np.float64))

    LOGGER.info("loaded %d enrolled face(s) from %s", len(labels), directory)
    return labels, vectors


def save(directory: str | Path, name: str, embedding: np.ndarray) -> Path:
    """Average-normalise and persist one embedding under ``name``.

    Overwrites an existing entry for the same name.
    """
    if name in names(directory):
        LOGGER.info("replacing existing enrollment for %r", name)

    vector = np.asarray(embedding, dtype=np.float64)
    if vector.shape != (EMBEDDING_DIMS,):
        raise FaceStoreError(f"embedding must be shape ({EMBEDDING_DIMS},), got {vector.shape}")

    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise FaceStoreError("refusing to store a zero-norm embedding")

    directory = faces_dir(directory)
    path = directory / f"{name}.npy"
    np.save(path, vector / norm)
    LOGGER.info("enrolled %r -> %s", name, path.name)
    return path


def mean_embedding(embeddings: list[np.ndarray]) -> np.ndarray:
    """Average a list of embeddings into a single unit-length vector."""
    if not embeddings:
        raise FaceStoreError("cannot average an empty list of embeddings")
    stack = np.asarray(embeddings, dtype=np.float64)
    mean = stack.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        raise FaceStoreError("averaged embedding has zero norm; embeddings disagree")
    return mean / norm