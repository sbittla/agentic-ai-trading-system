#!/bin/sh
# Regenerate the hashed lock files (SEC-10) for the Linux images (python:3.12-slim, x86_64).
# Works on any host with uv (pip install uv); no container needed:
#   cd requirements && sh compile.sh
set -e
UV="${UV:-uv}"
for f in base core notifier alerts console dev; do
  "$UV" pip compile --quiet --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
    --generate-hashes --emit-index-url "$f.in" -o "$f.lock"
done
# news: CPU-only torch from the PyTorch index (no CUDA packages in the image)
"$UV" pip compile --quiet --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --generate-hashes --emit-index-url --index-strategy unsafe-best-match news.in -o news.lock
