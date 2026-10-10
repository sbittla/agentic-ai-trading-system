"""Hardware detection and Qwen model selection (QW-1, QW-6; docs/HARDWARE.md).

The owner picks the Qwen model from what the machine can hold *fully in GPU memory*: a model
that spills onto the CPU makes calls take minutes and breaks the 60-second budget (QW-3).
``trader select-model`` detects the NVIDIA GPU and system RAM, shows which models fit, and can
write the choice to both places it must match: ``docker/.env`` (``OLLAMA_MODEL``, what is
downloaded) and ``config/config.yaml`` (``llm.model``, what trader-core asks for).
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# VRAM taken by the CUDA context, Ollama runtime buffers and fragmentation
RUNTIME_OVERHEAD_GB = 0.8
# headroom we always leave free on the card (driver, desktop compositor, other apps)
MIN_FREE_GB = 0.5


@dataclass(frozen=True)
class ModelProfile:
    tag: str                  # Ollama tag
    weights_gb: float         # download / in-memory size of the default Q4_K_M build
    kv_gb_8k: float           # KV cache at 8K context with OLLAMA_KV_CACHE_TYPE=q8_0
    tier: str
    use: str                  # what it is good for in this system

    def vram_needed_gb(self, ctx: int = 8192) -> float:
        return round(self.weights_gb + self.kv_gb_8k * ctx / 8192 + RUNTIME_OVERHEAD_GB, 1)


# Ordered from smallest to largest. Sizes are Ollama's Q4_K_M builds.
PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile("qwen3:1.7b", 1.4, 0.2, "minimum",
                 "development and replays only; too weak for trade review"),
    ModelProfile("qwen3:4b", 2.6, 0.3, "entry",
                 "4-6 GB GPUs; usable for summaries, marginal for strategy and review"),
    ModelProfile("qwen3:8b", 5.2, 0.6, "standard",
                 "8-11 GB GPUs (reference machine: RTX 4060 Laptop 8 GB); system default"),
    ModelProfile("qwen3:14b", 9.3, 0.8, "high",
                 "12-16 GB GPUs; better plans and reviews than 8B"),
    ModelProfile("qwen3:30b-a3b", 19.0, 0.4, "very high",
                 "24 GB GPUs when speed matters (--model); mixture-of-experts, 3B active"),
    ModelProfile("qwen3:32b", 20.0, 1.1, "maximum",
                 "24 GB+ GPUs; strongest dense model"),
)
DEFAULT_MODEL = "qwen3:8b"


def profile(tag: str) -> ModelProfile | None:
    return next((p for p in PROFILES if p.tag == tag), None)


@dataclass(frozen=True)
class Gpu:
    name: str
    total_gb: float
    used_gb: float

    @property
    def free_gb(self) -> float:
        return self.total_gb - self.used_gb


@dataclass(frozen=True)
class Hardware:
    gpus: tuple[Gpu, ...]     # NVIDIA only: CUDA (and so Ollama in Docker) cannot use others
    ram_gb: float | None
    cpu_threads: int | None

    @property
    def gpu(self) -> Gpu | None:
        """The NVIDIA GPU Ollama will use (the largest)."""
        return max(self.gpus, key=lambda g: g.total_gb, default=None)


def detect(run: object = subprocess.run) -> Hardware:
    return Hardware(_nvidia_gpus(run), _ram_gb(), os.cpu_count())


def _nvidia_gpus(run: object) -> tuple[Gpu, ...]:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return ()
    try:
        out = run([exe, "--query-gpu=name,memory.total,memory.used",   # type: ignore[operator]
                   "--format=csv,noheader,nounits"], capture_output=True, text=True,
                  timeout=15, check=True).stdout
    except Exception:   # noqa: BLE001 - driver missing or GPU busy: treat as no GPU
        return ()
    return parse_nvidia_smi(out)


def parse_nvidia_smi(text: str) -> tuple[Gpu, ...]:
    """``name, total MiB, used MiB`` per line (``--format=csv,noheader,nounits``)."""
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.rsplit(",", 2)]
        if len(parts) != 3:
            continue
        try:
            gpus.append(Gpu(parts[0], round(float(parts[1]) / 1024, 1),
                            round(float(parts[2]) / 1024, 1)))
        except ValueError:
            continue
    return tuple(gpus)


def _ram_gb() -> float | None:
    if os.name == "nt":
        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MemStatus()
        st.dwLength = ctypes.sizeof(MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):   # type: ignore[attr-defined]
            return round(float(st.ullTotalPhys) / 1024**3, 1)
        return None
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"MemTotal:\s+(\d+) kB", text)
    return round(int(m.group(1)) / 1024**2, 1) if m else None


@dataclass(frozen=True)
class Fit:
    profile: ModelProfile
    needed_gb: float
    status: str               # fits | tight | cpu-offload | no
    note: str


def assess(hw: Hardware, ctx: int = 8192) -> list[Fit]:
    """How each model would run on this machine."""
    gpu = hw.gpu
    out = []
    for p in PROFILES:
        need = p.vram_needed_gb(ctx)
        if gpu is not None:
            usable = gpu.total_gb - max(gpu.used_gb, MIN_FREE_GB)
            if need <= usable - 0.5:
                out.append(Fit(p, need, "fits", f"100% on GPU, {usable - need:.1f} GB spare"))
                continue
            if need <= usable:
                out.append(Fit(p, need, "tight", "fits only with nothing else on the GPU"))
                continue
        ram_ok = hw.ram_gb is not None and p.weights_gb + 4 <= hw.ram_gb * 0.5
        if gpu is None and ram_ok and p.weights_gb <= 3:
            out.append(Fit(p, need, "cpu-only",
                           "no NVIDIA GPU: runs on CPU, for development and replays only"))
        elif gpu is not None and ram_ok:
            out.append(Fit(p, need, "cpu-offload",
                           "partly on CPU: calls take minutes, breaks the 60 s limit (QW-3)"))
        else:
            out.append(Fit(p, need, "no", "does not fit"))
    return out


def recommend(hw: Hardware, ctx: int = 8192) -> tuple[ModelProfile, str]:
    """Largest model that fits fully in GPU memory with headroom; the 1.7B/4B fall-backs
    otherwise (development only without a GPU)."""
    fits = assess(hw, ctx)
    full = [f for f in fits if f.status == "fits"]
    if full:
        name = hw.gpu.name if hw.gpu else "the GPU"
        return full[-1].profile, f"largest model that fits fully in {name} with headroom"
    tight = [f for f in fits if f.status == "tight"]
    if tight:
        return tight[-1].profile, "fits only when nothing else uses the GPU"
    cpu = [f for f in fits if f.status == "cpu-only"]
    small = cpu[-1].profile if cpu else PROFILES[0]
    return small, ("no NVIDIA GPU found: CPU only, for development and replays (live trading "
                   "needs a GPU to meet the 60 s limit, QW-3)")


# --- writing the choice -------------------------------------------------------------------

def set_env_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def set_yaml_llm_model(text: str, tag: str) -> str:
    """Replace ``model:`` inside the top-level ``llm:`` block, keeping comments and layout."""
    out, in_llm, done = [], False, False
    for ln in text.splitlines():
        if re.match(r"^\S", ln):
            in_llm = ln.startswith("llm:")
        if in_llm and not done:
            m = re.match(r"^(\s+model:\s*)(\S+)(.*)$", ln)
            if m:
                ln = f"{m.group(1)}{tag}{m.group(3)}"
                done = True
        out.append(ln)
    if not done:
        raise ValueError("no llm.model line found in the config")
    return "\n".join(out) + "\n"


def configured_models(root: Path) -> tuple[str | None, str | None]:
    """(OLLAMA_MODEL in docker/.env, llm.model in config/config.yaml)."""
    env_model = cfg_model = None
    env = root / "docker" / ".env"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if ln.startswith("OLLAMA_MODEL="):
                env_model = ln.split("=", 1)[1].strip() or None
    cfg = root / "config" / "config.yaml"
    if cfg.exists():
        in_llm = False
        for ln in cfg.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\S", ln):
                in_llm = ln.startswith("llm:")
            m = re.match(r"^\s+model:\s*(\S+)", ln)
            if in_llm and m:
                cfg_model = m.group(1)
                break
    return env_model, cfg_model
