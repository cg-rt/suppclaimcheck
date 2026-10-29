"""Downloads the embedding model for hybrid retrieval (P2.06, D-019, D-046) into a folder, and checks it.

The model is fetched here, at build time (Docker build, CI, a dev machine), never by the running app:
core loads it from the folder with network access off, because only the gateway calls outside services.
The revision and every file's SHA-256 are pinned below, so a changed upload fails the build instead of
changing the rankings.

Standard library only, so the Docker build stage can run it before anything is installed.

Run: python tools/fetch_embedding_model.py [folder]   (default: .cache/models/bge-small-en-v1.5)
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / ".cache" / "models" / "bge-small-en-v1.5"

# fastembed's ONNX build of BAAI/bge-small-en-v1.5 (quantized, 384 dimensions).
REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
FILES = {
    "config.json": "13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd",
    "model_optimized.onnx": "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
    "special_tokens_map.json": "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",  # gitleaks:allow (a public file checksum, not a key)
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",  # gitleaks:allow (a public file checksum, not a key)
    "tokenizer_config.json": "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",  # gitleaks:allow (a public file checksum, not a key)
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = folder / name
        if target.exists() and _sha256(target) == expected:
            continue
        url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
        partial = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=60) as response:
            partial.write_bytes(response.read())
        actual = _sha256(partial)
        if actual != expected:
            partial.unlink()
            raise SystemExit(f"{name}: SHA-256 {actual}, expected {expected}")
        partial.replace(target)
        print(f"fetched {name}")
    print(f"embedding model ready in {folder}")


if __name__ == "__main__":
    fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT)
