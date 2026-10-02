"""Download an Ollama model on the host (outside Docker), resumable and sha256-verified.

Use when large downloads inside Docker Desktop are unreliable. The result is an Ollama models
folder that `tools/import_ollama_model.ps1` copies into the `ollama-models` volume, so the
runtime `ollama` container never needs internet access (SEC-9).

    python tools/fetch_ollama_model.py qwen3:8b data/ollama_models
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx

REGISTRY = "https://registry.ollama.ai"
ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_blob(client: httpx.Client, repo: str, digest: str, dest: Path, size: int) -> None:
    algo, hexd = digest.split(":")
    out = dest / f"{algo}-{hexd}"
    if out.exists() and out.stat().st_size == size and sha256_of(out) == hexd:
        print(f"  ok      {digest[:19]}… ({size / 1e9:.2f} GB, already present)")
        return
    part = out.with_suffix(".part")
    for attempt in range(50):
        have = part.stat().st_size if part.exists() else 0
        if have >= size:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", f"{REGISTRY}/v2/{repo}/blobs/{digest}", headers=headers,
                               follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                done = have if mode == "ab" else 0
                with part.open(mode) as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if done % (256 << 20) < (1 << 20):
                            print(f"  …       {digest[:19]}… {done / size:6.1%}", flush=True)
        except (httpx.HTTPError, OSError) as exc:
            print(f"  retry {attempt + 1}: {exc}", flush=True)
    if sha256_of(part) != hexd:
        part.unlink()
        raise SystemExit(f"sha256 mismatch for {digest}; deleted, rerun to download again")
    part.replace(out)
    print(f"  done    {digest[:19]}… ({size / 1e9:.2f} GB, sha256 verified)")


def main(model: str, out_dir: str) -> None:
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    repo = name if "/" in name else f"library/{name}"
    root = Path(out_dir)
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"Accept": ACCEPT}) as client:
        r = client.get(f"{REGISTRY}/v2/{repo}/manifests/{tag}", timeout=30)
        r.raise_for_status()
        manifest = r.json()
        layers = [manifest["config"], *manifest["layers"]]
        total = sum(int(x["size"]) for x in layers)
        print(f"{model}: {len(layers)} files, {total / 1e9:.2f} GB")
        for layer in layers:
            fetch_blob(client, repo, layer["digest"], blobs, int(layer["size"]))
    mdir = root / "manifests" / "registry.ollama.ai" / Path(repo) / tag
    mdir.parent.mkdir(parents=True, exist_ok=True)
    mdir.write_text(json.dumps(manifest), encoding="utf-8")
    print(f"ready: {root} (import with tools/import_ollama_model.ps1)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b",
         sys.argv[2] if len(sys.argv) > 2 else "data/ollama_models")
