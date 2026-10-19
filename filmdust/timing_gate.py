"""Opt-in timing report for a large JPEG.

Compares import → first before/after preview and import → full preview with a
10 second threshold. The comparison is a report. It is not a statement that
the threshold has been met, and the numbers move with the machine.
"""
import os
import platform
import time
from pathlib import Path

import numpy as np

from engine import QUICK_PREVIEW_EDGE, Photo

TARGET_SECONDS = 10.0


def machine_spec():
    cpu = "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    memory = None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        memory = meminfo.read_text(errors="replace").splitlines()[0]
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor() or cpu,
        "cpu": cpu,
        "cpu_count": os.cpu_count(),
        "memory": memory,
    }


def judge_times(first_compare_s, final_preview_s, target_s=TARGET_SECONDS):
    """Return pass/fail flags. Callers decide whether a miss is an error."""
    return {
        "target_s": target_s,
        "first_compare_within_target": bool(first_compare_s <= target_s),
        "final_preview_within_target": bool(final_preview_s <= target_s),
    }


def measure(path):
    """Time Photo import, default Auto, the first comparison, and the full preview.

    Default Auto matches the app: level auto, sensitivity 70, size 30, light
    dust, edge protection and hair detection. The clock starts at construction
    and includes decode, cleanup and preview encoding. It does not include a
    file dialog, browser decode or window draw.
    """
    path = Path(path)
    data = path.read_bytes()
    started = time.perf_counter()
    photo = Photo(data, path.name)
    photo.process("auto")
    region = np.s_[:, :]
    quick = QUICK_PREVIEW_EDGE if max(photo.height, photo.width) > QUICK_PREVIEW_EDGE else None
    for kind in ("original", "result", "overlay"):
        photo.render_preview(kind, region, quick, True)
    first_compare_s = time.perf_counter() - started
    for kind in ("original", "result", "overlay"):
        photo.render_preview(kind, region, None, True)
    final_preview_s = time.perf_counter() - started
    megapixels = photo.width * photo.height / 1_000_000
    report = {
        "sample": path.name,
        "width": photo.width,
        "height": photo.height,
        "megapixels": round(megapixels, 3),
        "depth": photo.depth,
        "quick_edge": quick,
        "first_compare_s": first_compare_s,
        "final_preview_s": final_preview_s,
        "near_60mp": bool(45.0 <= megapixels <= 80.0),
        "machine": machine_spec(),
        "note": "Times depend on this machine. A true comparison flag only describes this run; it does not record the 10 second target as met.",
    }
    report.update(judge_times(first_compare_s, final_preview_s))
    return report


def enforce_enabled(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("FILMDUST_TIMING_ENFORCE") == "1"
