"""What SprigCast costs the PC right now: its CPU share, its memory, and how busy the GPU is.

Shown in the status bar every two seconds so any change (a new source, Eco settings) shows its
effect straight away. The GPU reading comes from nvidia-smi on a background thread, since it can
take a moment; without an NVIDIA card it is simply left out.
"""

from __future__ import annotations

import os
import threading
import time

try:
    import psutil
except ImportError:  # optional: the meter shows nothing without it
    psutil = None

GPU_EVERY_S = 3.0


class ResourceMeter:
    def __init__(self, gpu: bool = True):
        self._process = psutil.Process(os.getpid()) if psutil else None
        self._cores = (psutil.cpu_count() or 1) if psutil else 1
        if self._process is not None:
            self._process.cpu_percent()  # the first call only starts the measurement
        self.gpu_pct: float | None = None
        self._stop = threading.Event()
        self._gpu_thread = None
        if gpu:
            self._gpu_thread = threading.Thread(target=self._poll_gpu, name="sprigcast-gpu-meter", daemon=True)
            self._gpu_thread.start()

    def _poll_gpu(self) -> None:
        from sprigcast.app.doctor import gpu_load

        while True:
            self.gpu_pct = gpu_load()
            if self.gpu_pct is None or self._stop.wait(GPU_EVERY_S):
                return  # no NVIDIA tools, or stopping

    def stop(self) -> None:
        self._stop.set()

    def sample(self) -> dict:
        """{"cpu": % of the whole PC, "ram_mb": resident memory, "gpu": % or None}; {} without psutil."""
        if self._process is None:
            return {}
        try:
            cpu = self._process.cpu_percent() / self._cores
            ram = self._process.memory_info().rss / 1e6
        except (psutil.Error, OSError):
            return {}
        return {"cpu": cpu, "ram_mb": ram, "gpu": self.gpu_pct, "at": time.monotonic()}


def describe(sample: dict) -> str:
    """"CPU 4% · RAM 312 MB · GPU 18%" for the status bar."""
    if not sample:
        return ""
    text = f"CPU {sample['cpu']:.0f}% · RAM {sample['ram_mb']:.0f} MB"
    if sample.get("gpu") is not None:
        text += f" · GPU {sample['gpu']:.0f}%"
    return text
