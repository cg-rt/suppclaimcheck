"""Passage and claim embeddings for hybrid retrieval (P2.06, D-019, D-046).

fastembed runs the model locally in core-worker; nothing leaves the machine. The model files come from
tools/fetch_embedding_model.py at build time and are loaded with network access off, because only the
gateway may call outside services (AGENTS.md section 6).

Vectors are cached in core.embeddings by the SHA-256 of the passage text and the model name, so an
article read again (by another check, or a rerun) is not embedded again.
"""

import asyncio
import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Protocol

from psycopg import AsyncConnection

# Where the model files are: the image sets this; a dev checkout falls back to the fetch tool's folder.
MODEL_DIR_VARIABLE = "SCC_EMBEDDING_MODEL_DIR"
_DEV_DIR = (
    Path(__file__).resolve().parents[5] / ".cache" / "models" / "bge-small-en-v1.5"
)


class Embedder(Protocol):
    name: str

    def passages(self, texts: list[str]) -> list[list[float]]: ...

    def query(self, text: str) -> list[float]: ...


class FastEmbedder:
    def __init__(self, name: str, folder: Path):
        # Imported here so core-api, which never embeds, doesn't load onnxruntime. onnxruntime ships a
        # Microsoft telemetry client: off here too, not only through ORT_DISABLE_TELEMETRY in the image.
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import onnxruntime
        from fastembed import TextEmbedding

        onnxruntime.disable_telemetry_events()

        self.name = name
        self.model = TextEmbedding(
            name, specific_model_path=str(folder), local_files_only=True
        )

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.passage_embed(texts, batch_size=32)]

    def query(self, text: str) -> list[float]:
        return next(iter(self.model.query_embed([text]))).tolist()


def model_dir() -> Path:
    return Path(os.environ.get(MODEL_DIR_VARIABLE) or _DEV_DIR)


@cache
def load(name: str) -> Embedder:
    folder = model_dir()
    if not (folder / "model_optimized.onnx").exists():
        raise FileNotFoundError(
            f"no embedding model in {folder}: run tools/fetch_embedding_model.py"
        )
    return FastEmbedder(name, folder)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def literal(vector: list[float]) -> str:
    """pgvector's text form; rounded so the same vector always writes the same value."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


async def ensure(conn: AsyncConnection, embedder: Embedder, texts: list[str]) -> int:
    """Stores a vector for every text that has none for this model yet; returns how many were embedded."""
    hashes = {text_hash(t): t for t in texts}
    cur = await conn.execute(
        "SELECT text_hash FROM core.embeddings WHERE model = %s AND text_hash = ANY(%s)",
        (embedder.name, list(hashes)),
    )
    cached = {row[0] for row in await cur.fetchall()}
    missing = [h for h in hashes if h not in cached]
    if not missing:
        return 0
    # The model is CPU-bound: run it off the event loop.
    vectors = await asyncio.to_thread(embedder.passages, [hashes[h] for h in missing])
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO core.embeddings (text_hash, model, embedding) VALUES (%s, %s, %s::vector)"
            " ON CONFLICT DO NOTHING",
            [
                (h, embedder.name, literal(v))
                for h, v in zip(missing, vectors, strict=True)
            ],
        )
    return len(missing)


def fuse(
    keyword_order: list[int], vector_order: list[int], weight: float, k: int
) -> list[int]:
    """Weighted reciprocal rank fusion: score = weight / (k + keyword rank) + (1 - weight) / (k + vector rank).

    Ranks start at 1. Ties go to the earlier passage in the article, so the order is reproducible.
    """
    if not 0 <= weight <= 1:
        raise ValueError(f"keyword weight must be 0-1, not {weight}")
    score: dict[int, float] = {}
    for w, order in ((weight, keyword_order), (1 - weight, vector_order)):
        for rank, ordinal in enumerate(order, start=1):
            score[ordinal] = score.get(ordinal, 0.0) + w / (k + rank)
    return sorted(score, key=lambda o: (-round(score[o], 12), o))
