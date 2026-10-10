# System Requirements, Reference Machine and Model Selection

**Version:** 1.0 (October 10, 2026) · Requirements QW-1, QW-6 ([REQUIREMENTS.md](REQUIREMENTS.md) §10.2)

This page covers the hardware the system needs, the machine it is set up and tested on, and how to choose the Qwen model for your own hardware.

## 1. Reference machine (set up and tested here)

Every setup step in [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) and [RUNBOOK.md](RUNBOOK.md), and every problem in [TROUBLESHOOTING.md](TROUBLESHOOTING.md), comes from this laptop:

| Part | Specification | Used for |
| --- | --- | --- |
| GPU 1 (discrete) | **NVIDIA GeForce RTX 4060 Laptop GPU, 8 GB dedicated**, driver 32.0.15.9282 (June 3, 2026), DirectX 12 FL 12.2, PCI bus 1 | `ollama` container: Qwen3 runs here, fully in GPU memory |
| GPU 0 (integrated) | Intel UHD Graphics, driver 32.0.101.6790 (April 28, 2025), shares system RAM | Windows desktop only. **Not used by the system**: Docker GPU passthrough is CUDA (NVIDIA) only |
| CPU | 13th Gen Intel Core i7-13700HX: 16 cores / 24 threads, 2.1 GHz base, 30 MB L3, virtualization on | trader-core, news-sentiment (FinBERT on CPU), Postgres, notifier |
| Memory | 32 GB DDR5-4800 (2 × SODIMM, both slots used) | Windows + the WSL2 VM that runs Docker |
| OS / runtime | Windows with Docker Desktop (WSL2 backend), NVIDIA WSL GPU support from the Windows driver | all containers |

At idle with normal desktop use, the readings were:

- RTX 4060: 0.5 of 8.0 GB used.
- Memory: 13.5 GB available, 26.2 of 31.7 GB committed.

The budgets in §2 are built from these numbers.

## 2. How the reference machine is configured

### GPU memory budget: why Qwen3 8B

| Item | GB |
| --- | --- |
| RTX 4060 Laptop dedicated memory | 8.0 |
| Already used by Windows / driver | −0.5 |
| Qwen3 8B weights (Q4_K_M) | −5.2 |
| KV cache, 8K context, 8-bit (`OLLAMA_KV_CACHE_TYPE=q8_0`) | −0.6 |
| CUDA context and Ollama buffers | −0.8 |
| **Spare** | **0.9** |

Qwen3 14B needs about 10.9 GB, so on this card part of it would run on the CPU. Calls would then take minutes and break the 60-second limit (QW-3). That is why the default is `qwen3:8b`.

[docker/docker-compose.yml](../docker/docker-compose.yml) sizes Ollama for this card:

- one loaded model (`OLLAMA_MAX_LOADED_MODELS=1`)
- one request at a time (`OLLAMA_NUM_PARALLEL=1`)
- flash attention, which the 8-bit KV cache needs
- the model kept loaded for 8 hours

### CPU and RAM budget

| Consumer | Approx. RAM | Notes |
| --- | --- | --- |
| Windows, browser and other apps (measured) | ~18 GB | 13.5 GB was available before starting Docker |
| WSL2 VM cap (`.wslconfig`) | 12 GB + 8 GB swap | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §1.1 step 4. Without a cap the VM grabs memory and Docker Desktop crashed |
| ↳ news-sentiment (PyTorch + FinBERT) | 1.5–2 GB | CPU inference, threads capped at 4 (`OMP_NUM_THREADS`) |
| ↳ trader-core | 0.3–0.6 GB | |
| ↳ ollama (host side; weights are in VRAM) | 1–2 GB | |
| ↳ postgres, notifier, backup | ~0.5 GB | |
| ↳ grafana + proxy (optional profile) | ~0.3 GB | |
| alerts only (`--profile alerts`) | ~0.05 GB | needs nothing else ([ALERTS.md](ALERTS.md)) |

The CPU caps (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` = 4) keep the Python services off the other 20 threads. The Windows page file must stay on ([TROUBLESHOOTING.md](TROUBLESHOOTING.md) §1.1 step 0).

## 3. How it is tested

| Level | Where | Command | Pass |
| --- | --- | --- | --- |
| Unit, replay, contract, alerts | Any machine and GitHub CI (no GPU, fake LLM) | `pytest -q` | all pass |
| GPU passthrough | Reference machine | `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` | shows the RTX 4060 Laptop GPU |
| Model choice | Reference machine (Windows host) | `trader select-model` | recommends `qwen3:8b`, status `fits` |
| Model on GPU, JSON output, latency | Reference machine, inside trader-core | `trader check-connections --service llm` | `llm model`, `llm generate` and `llm gpu 100% on GPU` all PASS |
| Whole system on synthetic days | Reference machine | `trader replay --synthetic 10 --start 2026-10-05 --llm ollama` | completes; LLM p95 latency under 60 s |
| Paper trading | Reference machine | [TEST_PLAN.md](TEST_PLAN.md) stage M8 | as defined there |

When you change the model or move to another machine, re-run the last four rows and record the result in [BUILD_STATUS.md](BUILD_STATUS.md).

## 4. System requirements

| Use | GPU | RAM | CPU | Disk |
| --- | --- | --- | --- | --- |
| Alerts only ([ALERTS.md](ALERTS.md)) | none | 1 GB | any | < 1 GB |
| Development, replays and backtests (`--llm rules`/`cached`) | none | 8 GB | 4 cores | 10 GB |
| **Paper / live trading, minimum** | NVIDIA, **8 GB VRAM** (CUDA, recent driver with WSL support) | 16 GB | 6 cores | 30 GB |
| **Paper / live trading, reference** | RTX 4060 Laptop 8 GB | 32 GB | i7-13700HX (16C/24T) | 30 GB+ |
| Higher-quality model | NVIDIA 12–16 GB (14B) or 24 GB (32B) | 32 GB+ | 8+ cores | 50 GB |

Integrated Intel or AMD graphics cannot run the model in Docker. On a laptop with two GPUs, the NVIDIA card is the one that counts.

## 5. Choosing the model for your hardware (QW-6)

Supported Qwen3 models. "Needs" is the GPU memory at 8K context with the compose settings above:

| Model | Download | Needs | Pick it when | Quality for this system |
| --- | --- | --- | --- | --- |
| `qwen3:1.7b` | 1.4 GB | 2.4 GB | no GPU, development only | too weak to review trades |
| `qwen3:4b` | 2.6 GB | 3.7 GB | 4–6 GB GPU, or CPU-only development | summaries fine; strategy and review marginal |
| **`qwen3:8b`** | 5.2 GB | 6.6 GB | **8–11 GB GPU (reference machine)** | **default** |
| `qwen3:14b` | 9.3 GB | 10.9 GB | 12–16 GB GPU | better plans and reviews |
| `qwen3:30b-a3b` | 19 GB | 20.2 GB | 24 GB GPU when speed matters (`--model qwen3:30b-a3b`) | strong, fast (mixture of experts) |
| `qwen3:32b` | 20 GB | 21.9 GB | 24 GB+ GPU | strongest |

Let the tool decide. Run it on the host that has the GPU, because it reads `nvidia-smi`:

```powershell
trader select-model                         # detect GPU + RAM, show the table, recommend
trader select-model --apply                 # write the recommendation to docker/.env and config/config.yaml
trader select-model --model qwen3:4b --apply   # choose a different supported model
trader select-model --vram 16 --ram 64      # plan for another machine (no detection)
```

The tool:

- recommends the largest model that fits fully in GPU memory with 0.5 GB to spare
- refuses (unless you add `--force`) a model that would spill onto the CPU
- warns if `OLLAMA_MODEL` and `llm.model` differ: trader-core would ask for a model that was never downloaded

Then:

1. Download the model: RUNBOOK §1 step 6, or `tools/fetch_ollama_model.py <tag>` on Windows.
2. Restart `ollama` and `trader-core`.
3. Check that `trader check-connections --service llm` shows `100% on GPU`.

Model changes take effect the next trading day (SR-5), like any other config change.

Lowering `llm.max_context_tokens` reduces the KV cache, so `--ctx 4096` frees about 0.3 GB on the 8B model. Do not go below what the prompts need ([AGENT_PROMPTS.md](AGENT_PROMPTS.md)).

FinBERT (news sentiment) always runs on the CPU and needs no choice.
