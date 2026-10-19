"""Opt-in timing report for a large JPEG.

Compares import → first scaled before/after preview and import → full preview
with a 10 second threshold. The comparison is a report. It is not a statement
that the threshold has been met, and the numbers move with the machine.
"""
import hashlib
import os
import platform
import time
from pathlib import Path

import cv2
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


def mask_agreement(low_mask, full_mask):
    """Cell agreement of a low-res selection against the final selection.

    The final mask is reduced to the low-res grid. A cell counts as finally
    selected when any of its source pixels were selected. Recall is the share
    of those cells the low-res pass also selected. Candidate counts are not
    included and are not a quality score.
    """
    low_bin = np.asarray(low_mask) > 0
    full = np.asarray(full_mask)
    if low_bin.shape != full.shape[:2]:
        reduced = cv2.resize((full > 0).astype(np.uint8), (low_bin.shape[1], low_bin.shape[0]), interpolation=cv2.INTER_AREA)
        full_bin = reduced > 0
    else:
        full_bin = full > 0
    intersection = int(np.count_nonzero(low_bin & full_bin))
    low_count = int(np.count_nonzero(low_bin))
    final_count = int(np.count_nonzero(full_bin))
    union = int(np.count_nonzero(low_bin | full_bin))
    return {
        "recall": (intersection / final_count) if final_count else 1.0,
        "precision": (intersection / low_count) if low_count else 1.0,
        "iou": (intersection / union) if union else 1.0,
        "low_selected_cells": low_count,
        "final_selected_cells": final_count,
        "intersection_cells": intersection,
    }


def measure(path):
    """Time Photo import, the scaled first comparison, and the full preview.

    Default Auto matches the app: level auto, sensitivity 70, size 30, light
    dust, edge protection and hair detection. On a large scan the first
    comparison is Auto on the downscaled working image. Full Auto then replaces
    it. The clock starts at construction and includes decode, cleanup and
    preview encoding. It does not include a file dialog, browser decode or
    window draw.
    """
    path = Path(path)
    data = path.read_bytes()
    started = time.perf_counter()
    photo = Photo(data, path.name)
    if photo.quick_scale < 1:
        photo.prepare_quick_compare("auto")
        low_mask = None if photo.quick_selected is None else photo.quick_selected.copy()
    else:
        photo.process("auto")
        low_mask = photo.mask
    region = np.s_[:, :]
    quick = QUICK_PREVIEW_EDGE if max(photo.height, photo.width) > QUICK_PREVIEW_EDGE else None
    for kind in ("original", "result", "overlay"):
        photo.render_preview(kind, region, quick, True)
    first_compare_s = time.perf_counter() - started
    if photo.quick_compare_pending_full:
        photo.process("auto")
    for kind in ("original", "result", "overlay"):
        photo.render_preview(kind, region, None, True)
    final_preview_s = time.perf_counter() - started
    summary = photo.quick_summary or {}
    megapixels = photo.width * photo.height / 1_000_000
    report = {
        "sample": path.name,
        "width": photo.width,
        "height": photo.height,
        "megapixels": round(megapixels, 3),
        "depth": photo.depth,
        "quick_edge": QUICK_PREVIEW_EDGE,
        "quick_scale": float(photo.quick_scale),
        "light_median": summary.get("light_median"),
        "scaled_size": summary.get("scaled_size"),
        "scaled_length": summary.get("scaled_length"),
        "split_median": summary.get("split_median"),
        "first_compare_s": first_compare_s,
        "final_preview_s": final_preview_s,
        "near_60mp": bool(45.0 <= megapixels <= 80.0),
        "machine": machine_spec(),
        "note": "Times depend on this machine. A true comparison flag only describes this run; it does not record the 10 second target as met.",
    }
    report.update(mask_agreement(photo.mask if low_mask is None else low_mask, photo.mask))
    report.update(judge_times(first_compare_s, final_preview_s))
    # After the clock. This is the full Auto export, including when a scaled
    # compare ran first. A full-only baseline must hash to the same value.
    report["tiff_sha256"] = hashlib.sha256(photo.export("tiff")[0]).hexdigest()
    return report


def enforce_enabled(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("FILMDUST_TIMING_ENFORCE") == "1"
