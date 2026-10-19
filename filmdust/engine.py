"""Local, mask-only film dust detection and healing. No model downloads."""
import io
import math
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import cv2
import numpy as np
import tifffile
from PIL import Image, ImageCms, ImageOps

MAX_PIXELS = 100_000_000
# Longest edge of the first comparison. Full-frame previews replace it later.
QUICK_PREVIEW_EDGE = 2048


class Photo:
    def __init__(self, data, name):
        self.name = Path(name).name
        self.icc = None
        self.dpi = (300, 300)
        self.notes = []
        self.associated_alpha = False
        if data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            with tifffile.TiffFile(io.BytesIO(data)) as tf:
                if len(tf.pages) != 1:
                    raise ValueError("暂不支持多页 TIFF，请先导出单张照片。")
                page = tf.pages[0]
                if page.imagewidth * page.imagelength > MAX_PIXELS:
                    raise ValueError("照片超过 1 亿像素，请先裁切或缩小。")
                if page.photometric.name not in ("RGB", "MINISBLACK", "MINISWHITE"):
                    raise ValueError("暂不支持此 TIFF 色彩格式，请导出 RGB 或灰度 TIFF。")
                pixels = page.asarray()
                if page.planarconfig == 2 and pixels.ndim == 3:
                    pixels = np.moveaxis(pixels, 0, -1)
                if pixels.dtype not in (np.dtype("uint8"), np.dtype("uint16")):
                    raise ValueError("请使用 8 位或 16 位整数 TIFF。")
                if page.photometric.name == "MINISWHITE":
                    pixels = pixels.copy()
                    if pixels.ndim == 2:
                        pixels = np.iinfo(pixels.dtype).max - pixels
                    else:
                        pixels[:, :, 0] = np.iinfo(pixels.dtype).max - pixels[:, :, 0]
                if page.extrasamples:
                    if len(page.extrasamples) != 1 or page.extrasamples[0].name not in ("ASSOCALPHA", "UNASSALPHA"):
                        raise ValueError("暂不支持此 TIFF 附加通道，请导出 RGB 或标准透明度 TIFF。")
                    self.associated_alpha = page.extrasamples[0].name == "ASSOCALPHA"
                if "InterColorProfile" in page.tags:
                    self.icc = bytes(page.tags["InterColorProfile"].value)
                orientation = page.tags.get("Orientation")
                orientation = orientation.value if orientation else 1
                pixels = orient_array(pixels, orientation)
                if "XResolution" in page.tags and "YResolution" in page.tags:
                    xy = [page.tags[t].value for t in ("XResolution", "YResolution")]
                    self.dpi = tuple(a / b for a, b in xy)
                    if page.tags.get("ResolutionUnit") and page.tags["ResolutionUnit"].value == 3:
                        self.dpi = tuple(v * 2.54 for v in self.dpi)
        else:
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > MAX_PIXELS:
                    raise ValueError("照片超过 1 亿像素，请先裁切或缩小。")
                if getattr(im, "n_frames", 1) > 1:
                    raise ValueError("暂不支持动图，请使用单张照片。")
                self.icc = im.info.get("icc_profile")
                self.dpi = im.info.get("dpi", (300, 300))
                if data[:8] == b"\x89PNG\r\n\x1a\n" and data[24] == 16:
                    # Pillow reduces multi-channel 16-bit PNGs to 8 bits.
                    pixels = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
                    if pixels is None or pixels.dtype != np.uint16:
                        raise ValueError("无法完整读取此 16 位 PNG，请使用 TIFF。")
                    if pixels.ndim == 3:
                        color_type = data[25]
                        pixels = pixels[:, :, [0, 3]] if color_type == 4 else pixels[:, :, [2, 1, 0, 3]] if pixels.shape[2] == 4 else pixels[:, :, ::-1]
                    pixels = orient_array(pixels, im.getexif().get(274, 1))
                elif im.mode in ("I", "I;16", "I;16B", "I;16L"):
                    pixels = np.asarray(ImageOps.exif_transpose(im))
                    if np.min(pixels) < 0 or np.max(pixels) > 65535:
                        raise ValueError("灰度数值超出 16 位范围。")
                    pixels = pixels.astype(np.uint16)
                else:
                    if im.mode not in ("RGB", "RGBA", "L", "LA"):
                        raise ValueError("请先将图片导出为 RGB 或灰度格式。")
                    pixels = np.asarray(ImageOps.exif_transpose(im))
        if pixels.ndim not in (2, 3) or (pixels.ndim == 3 and pixels.shape[2] not in (2, 3, 4)):
            raise ValueError("不支持此图像通道格式。")
        if min(pixels.shape[:2]) < 32:
            raise ValueError("照片太小，最短边至少需要 32 像素。")
        self.original = np.ascontiguousarray(pixels)
        try:
            dpi = tuple(float(v) for v in self.dpi)
            self.dpi = dpi if len(dpi) == 2 and all(math.isfinite(v) and v > 0 for v in dpi) else (300., 300.)
        except (TypeError, ValueError):
            self.dpi = (300., 300.)
        self.height, self.width = pixels.shape[:2]
        self.depth = pixels.dtype.itemsize * 8
        self.gray = pixels.ndim == 2 or pixels.shape[2] == 2
        self.alpha = pixels[:, :, -1] if pixels.ndim == 3 and pixels.shape[2] in (2, 4) else None
        self.mask = np.zeros((self.height, self.width), np.uint8)
        self.pending_mask = np.zeros_like(self.mask)
        self.detection_report = None
        self.history = []
        self.clone_strokes = []
        # Original pixel scans remain valid when selection/repair edits change.
        # RAM only, scoped to this imported photo; never written to disk.
        self.proposal_cache = OrderedDict()
        self.heal_report = None
        self.revision = 0
        self.healed_revision = -1
        self.result = None
        self.preview = self.display(self.original)
        self.preview_patch = None
        self.edit_report = None
        # Reads of the cached original preview must stay valid while detect runs.
        # The cache is immutable after this point; detect only reads self.preview.
        self.lock = threading.RLock()
        self.quick_original = None
        self.quick_original_is_full = False
        self._cache_quick_original()

    def _cache_quick_original(self):
        """Lossy low-resolution original, ready before detection starts.

        Large scans use the same downscale-then-display path as the quick result,
        so the first comparison does not compare two different color pipelines.
        """
        if max(self.height, self.width) <= QUICK_PREVIEW_EDGE:
            displayed = self.preview
            self.quick_original_is_full = True
        else:
            displayed = self.display(downscale(self.original, QUICK_PREVIEW_EDGE))
            self.quick_original_is_full = False
        self.quick_original = lossy_preview_bytes(displayed)

    def _quick_original_matches(self, max_edge):
        if self.quick_original is None:
            return False
        if max_edge is None:
            return self.quick_original_is_full
        long_edge = max(self.height, self.width)
        return max_edge == QUICK_PREVIEW_EDGE or (self.quick_original_is_full and max_edge >= long_edge)

    def render_preview(self, kind, region, max_edge=None, full=True):
        """Lossy bytes for /api/image. Does not change detection or heal pixels."""
        detailed = not full
        if kind == "original":
            if full and self._quick_original_matches(max_edge):
                return self.quick_original
            pixels = self.preview[region]
            if max_edge:
                pixels = downscale(pixels, max_edge)
            return lossy_preview_bytes(pixels, detailed=detailed)
        if kind == "mask":
            pixels = self.mask[region]
            if max_edge:
                pixels = downscale(pixels, max_edge)
            return lossy_preview_bytes(pixels, detailed=detailed)
        if kind == "overlay":
            mask = self.mask[region]
            pending = self.pending_mask[region]
            if max_edge:
                mask = downscale(mask, max_edge)
                pending = downscale(pending, max_edge)
            overlay = np.zeros((mask.shape[0], mask.shape[1], 4), np.uint8)
            overlay[pending > 0] = [242, 195, 79, 180]
            overlay[mask > 0] = [244, 91, 66, 155]
            return lossy_preview_bytes(overlay, detailed=detailed)
        if kind == "result":
            pixels = self.heal()[region]
            if max_edge:
                pixels = downscale(pixels, max_edge)
            return lossy_preview_bytes(self.display(pixels), detailed=detailed)
        raise ValueError("未知预览类型。")

    @contextmanager
    def transaction(self):
        """An edit and its repair are one operation, including undo on failure."""
        saved = {key: getattr(self, key) for key in (
            "mask", "pending_mask", "detection_report", "history", "clone_strokes",
            "revision", "healed_revision", "result", "heal_report", "preview_patch", "edit_report")}
        self.mask = self.mask.copy()
        self.history = list(self.history)
        try:
            yield
            self.heal()
        except Exception:
            for key, value in saved.items():
                setattr(self, key, value)
            raise

    def straight_pixels(self, pixels):
        if not self.associated_alpha:
            return pixels
        result = pixels.copy()
        maximum = np.iinfo(pixels.dtype).max
        alpha = pixels[:, :, -1:].astype(np.float32)
        result[:, :, :-1] = np.clip(np.round(pixels[:, :, :-1].astype(np.float32) * maximum / np.maximum(alpha, 1)), 0, maximum).astype(pixels.dtype)
        return result

    def repair(self, mask, report):
        result = heal_mask(self.straight_pixels(self.original), mask, self.gray, report)
        if self.associated_alpha:
            selected = mask > 0
            maximum = np.iinfo(result.dtype).max
            values = np.round(result[selected, :-1].astype(np.float32) * self.alpha[selected, None] / maximum).astype(result.dtype)
            result = self.original.copy()
            result[selected, :-1] = values
        return result

    def checkpoint(self):
        # Pixel snapshots are immutable. Local edits copy the result once, never
        # replay thousands of automatic repairs, and undo restores exact pixels.
        self.history.append(dict(mask=self.mask.copy(), clone_strokes=list(self.clone_strokes),
                                 pending_mask=self.pending_mask, detection_report=self.detection_report,
                                 result=self.heal(), heal_report=self.heal_report,
                                 revision=self.revision, edit_report=self.edit_report, bounds=None))
        # Bound retained document states, but keep at least the previous state.
        per_state = self.original.nbytes + 2 * self.mask.nbytes
        max_history = max(1, min(12, (256 * 1024 * 1024) // per_state))
        self.history = self.history[-max_history:]

    def changed(self):
        self.revision += 1
        self.result = None
        self.heal_report = None
        self.preview_patch = None
        self.edit_report = None

    def finish_local_edit(self, kind, paint=None, clone=None):
        """Apply only this explicit user action to the last displayed pixels."""
        before = self.history[-1]
        if np.count_nonzero(self.mask) / self.mask.size > .15:
            raise ValueError("选区超过照片的 15%，请先减少选区；此工具适合小灰尘和细丝。")
        changed = cv2.bitwise_or(cv2.absdiff(self.mask, before['mask']),
                                 cv2.absdiff(self.pending_mask, before['pending_mask']))
        if paint is not None:
            changed = cv2.bitwise_or(changed, paint)
        x, y, w, h = cv2.boundingRect(changed)
        self.result = before['result'].copy() if w and h else before['result']
        report = dict(before['heal_report'] or dict(local=0, edge=0, donor=0, pending=[]))
        pending = []
        for px, py in report.get('pending', []):
            px, py = int(np.clip(px, 0, self.width - 1)), int(np.clip(py, 0, self.height - 1))
            if not before['mask'][py, px]:
                x0, y0 = max(0, px - 32), max(0, py - 32)
                yy, xx = np.nonzero(before['mask'][y0:py + 33, x0:px + 33])
                if not len(xx):
                    continue
                j = np.argmin((xx + x0 - px) ** 2 + (yy + y0 - py) ** 2)
                px, py = int(xx[j] + x0), int(yy[j] + y0)
            if self.mask[py, px] and (paint is None or not paint[py, px]):
                pending.append([px, py])
        report['pending'] = pending
        if w and h:
            box = np.s_[y:y + h, x:x + w]
            removed = (before['mask'][box] > 0) & (self.mask[box] == 0)
            self.result[box][removed] = self.original[box][removed]
            if paint is not None and clone is None and np.any(paint):
                # Include clean surrounding pixels for interpolation, while
                # committing exclusively the user-confirmed component/stroke.
                pad = 64
                x0, y0 = max(0, x - pad), max(0, y - pad)
                x1, y1 = min(self.width, x + w + pad), min(self.height, y + h + pad)
                region = np.s_[y0:y1, x0:x1]
                mask = paint[region]
                local_report = {}
                source = self.straight_pixels(self.result[region])
                healed = heal_mask(source, mask, self.gray, local_report, manual=True)
                selected = mask > 0
                if self.associated_alpha:
                    maximum = np.iinfo(healed.dtype).max
                    healed[:, :, :-1] = np.round(healed[:, :, :-1].astype(np.float32) * healed[:, :, -1:] / maximum).astype(healed.dtype)
                self.result[region][selected] = healed[selected]
                for key in ('local', 'edge', 'donor', 'manual_fallback'):
                    report[key] = report.get(key, 0) + local_report.get(key, 0)
            if clone is not None:
                yy, xx = np.nonzero(clone['mask'])
                xx, yy = xx + clone['x0'], yy + clone['y0']
                self.result[yy, xx] = self.original[yy + clone['dy'], xx + clone['dx']]
                if self.alpha is not None:
                    if self.associated_alpha:
                        donor_alpha = self.alpha[yy + clone['dy'], xx + clone['dx']].astype(np.float32)
                        values = self.result[yy, xx, :-1].astype(np.float32) * self.alpha[yy, xx, None] / np.maximum(donor_alpha[:, None], 1)
                        self.result[yy, xx, :-1] = np.minimum(np.round(values), self.alpha[yy, xx, None]).astype(self.original.dtype)
                    self.result[yy, xx, -1] = self.alpha[yy, xx]
            delta = self.result[box] != before['result'][box]
            pixels = int(np.count_nonzero(np.any(delta, axis=2) if delta.ndim == 3 else delta))
        else:
            pixels = 0
        bounds = [int(x), int(y), int(w), int(h)]
        before['bounds'] = bounds
        self.preview_patch = dict(from_revision=before['revision'], bounds=bounds)
        self.edit_report = dict(kind=kind, changed_pixels=pixels)
        self.heal_report = report
        self.healed_revision = self.revision

    def display(self, pixels):
        pixels = self.straight_pixels(pixels)
        small = (pixels // 257).astype(np.uint8) if pixels.dtype == np.uint16 else pixels
        if self.gray:
            gray = small if small.ndim == 2 else small[:, :, 0]
            rgb = np.repeat(gray[:, :, None], 3, axis=2)
        else:
            rgb = small[:, :, :3]
        # Browser previews are sRGB; exported pixels keep their original profile.
        if self.icc and not self.gray:
            try:
                rgb = np.asarray(ImageCms.profileToProfile(
                    Image.fromarray(rgb), ImageCms.ImageCmsProfile(io.BytesIO(self.icc)),
                    ImageCms.createProfile("sRGB"), outputMode="RGB"))
            except Exception:
                if not self.notes:
                    self.notes.append("此色彩配置无法用于预览；导出仍保留原配置。")
        return np.ascontiguousarray(rgb)

    def info(self):
        n, labels, stats, _ = cv2.connectedComponentsWithStats(self.mask, 8)
        selected = int(np.count_nonzero(self.mask))
        pn, plabels, pstats, pcenters = cv2.connectedComponentsWithStats(self.pending_mask, 8)
        pending_points = []
        for k in range(1, pn):
            x, y, w, h, _ = pstats[k]
            yy, xx = np.nonzero(plabels[y:y + h, x:x + w] == k)
            cx, cy = pcenters[k]
            nearest = np.argmin((xx + x - cx) ** 2 + (yy + y - cy) ** 2)
            pending_points.append([int(xx[nearest] + x), int(yy[nearest] + y)])
        pending_count = pn - 1
        reviews = [dict(point=p, kind="candidate") for p in pending_points]
        repair_ids = set()
        for px, py in (self.heal_report or {}).get("pending", []):
            px, py = int(np.clip(px, 0, self.width - 1)), int(np.clip(py, 0, self.height - 1))
            k = int(labels[py, px])
            if not k:
                y0, x0 = max(0, py - 32), max(0, px - 32)
                yy, xx = np.nonzero(labels[y0:py + 33, x0:px + 33])
                if not len(xx):
                    continue
                nearest = np.argmin((xx + x0 - px) ** 2 + (yy + y0 - py) ** 2)
                k = int(labels[yy[nearest] + y0, xx[nearest] + x0])
            if k in repair_ids:
                continue
            repair_ids.add(k)
            x, y, w, h, _ = stats[k]
            yy, xx = np.nonzero(labels[y:y + h, x:x + w] == k)
            nearest = np.argmin((xx + x - px) ** 2 + (yy + y - py) ** 2)
            reviews.append(dict(point=[int(xx[nearest] + x), int(yy[nearest] + y)], kind="repair"))
        repaired = max(0, n - 1 - len(repair_ids)) if self.heal_report is not None else 0
        changes = []
        for k in range(1, n):
            if k in repair_ids:
                continue
            x, y, w, h, _ = stats[k]
            yy, xx = np.nonzero(labels[y:y + h, x:x + w] == k)
            j = np.argmin((xx - w / 2) ** 2 + (yy - h / 2) ** 2)
            changes.append(dict(point=[int(x + xx[j]), int(y + yy[j])], kind="processed"))
        return dict(name=self.name, width=self.width, height=self.height, depth=self.depth,
                    gray=self.gray, revision=self.revision, undo=bool(self.history),
                    count=max(0, n - 1), pixels=selected,
                    percent=round(selected * 100 / self.mask.size, 4), notes=self.notes,
                    profile="原色彩配置已保留" if self.icc else "原图未嵌入色彩配置",
                    healing=self.heal_report, pending_count=int(pending_count), pending_points=pending_points, detection=self.detection_report,
                    processed_count=int(repaired), reviews=reviews, changes=changes,
                    preview_patch=self.preview_patch, edit=self.edit_report)

    def process(self, level="gentle", options=None):
        if level not in ("auto", "gentle", "standard", "more"):
            raise ValueError("无法识别去除程度。")
        options = options or {}
        sensitivity = options.get("sensitivity", {"auto":70, "gentle":40, "standard":50, "more":65}[level])
        key = (float(sensitivity), int(options.get("size", 30)), bool(options.get("hairs", True)),
               bool(options.get("protect", True)), options.get("polarity", "light"))
        proposal = self.proposal_cache.get(key)
        if proposal is None:
            proposal = detect_dust(self.preview, *key)
            if self.alpha is not None:
                proposal[self.alpha == 0] = 0
            if proposal.nbytes <= 32 * 1024 * 1024:
                self.proposal_cache[key] = proposal
                while len(self.proposal_cache) > 2 or sum(m.nbytes for m in self.proposal_cache.values()) > 32 * 1024 * 1024:
                    self.proposal_cache.popitem(last=False)
        selected, pending, summary = split_automatic_candidates(self.preview, proposal, level, key[-1])
        summary.update(level=level, sensitivity=float(sensitivity), size=key[1], automatic=True, custom=bool(options),
                       settings=dict(sensitivity=key[0], size=key[1], hairs=key[2], protect=key[3], polarity=key[4]))
        healing = {}
        result = self.repair(selected, healing)
        self.install_detection(selected, pending, summary)
        self.heal_report = healing
        self.result = result
        self.healed_revision = self.revision

    def detect(self, sensitivity=50, size=30, hairs=True, protect=True, polarity="both"):
        key = (float(sensitivity), int(size), bool(hairs), bool(protect), polarity)
        mask = self.proposal_cache.get(key)
        if mask is None:
            mask = detect_dust(self.preview, *key)
            if mask.nbytes <= 32 * 1024 * 1024:
                self.proposal_cache[key] = mask
                while len(self.proposal_cache) > 2 or sum(m.nbytes for m in self.proposal_cache.values()) > 32 * 1024 * 1024:
                    self.proposal_cache.popitem(last=False)
        self.install_detection(mask, np.zeros_like(mask), None)

    def install_detection(self, mask, pending, report):
        if mask.shape != self.mask.shape or pending.shape != self.mask.shape:
            raise ValueError("识别结果尺寸不符。")
        self.checkpoint()
        self.mask = mask.copy()
        self.pending_mask = pending.copy()
        self.detection_report = report
        self.clone_strokes = []
        if self.alpha is not None:
            self.mask[self.alpha == 0] = 0
        self.changed()

    def edit(self, mode, points, diameter):
        if mode not in ("add", "erase", "reject"):
            raise ValueError("未知画笔模式。")
        if not points or len(points) > 12000:
            raise ValueError("画笔路径为空或过长。")
        coords = []
        for x, y in points:
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("画笔坐标无效。")
            coords.append((int(np.clip(x, 0, self.width - 1)), int(np.clip(y, 0, self.height - 1))))
        self.checkpoint()
        radius = max(1, min(250, round(float(diameter) / 2)))
        if mode == "add":
            paint = np.zeros_like(self.mask)
            for point in coords:
                cv2.circle(paint, point, radius, 255, -1)
            for p, q in zip(coords, coords[1:]):
                cv2.line(paint, p, q, 255, radius * 2)
            # Touching an existing candidate confirms its whole connected defect,
            # including a defect that the automatic repair had left unchanged.
            union = cv2.bitwise_or(self.mask, self.pending_mask)
            remaining = union.copy()
            yy, xx = np.nonzero(cv2.bitwise_and(union, paint))
            for x, y in zip(xx, yy):
                if remaining[y, x]:
                    cv2.floodFill(remaining, None, (int(x), int(y)), 0)
            paint = cv2.bitwise_or(paint, cv2.subtract(union, remaining))
            if self.alpha is not None:
                paint[self.alpha == 0] = 0
            self.mask = cv2.bitwise_or(self.mask, paint)
            self.pending_mask = cv2.bitwise_and(self.pending_mask, cv2.bitwise_not(paint))
            self.changed()
            self.finish_local_edit("add", paint)
            return
        if mode == "reject":
            x, y = coords[0]
            if self.pending_mask[y, x]:
                remaining = self.pending_mask.copy()
                cv2.floodFill(remaining, None, (x, y), 0)
                self.pending_mask = remaining
                self.changed()
                self.finish_local_edit("reject")
                return
            a, b, c, d = max(0, x - radius), max(0, y - radius), min(self.width, x + radius + 1), min(self.height, y + radius + 1)
            yy, xx = np.nonzero(self.mask[b:d, a:c])
            if len(xx):
                idx = np.argmin((xx + a - x) ** 2 + (yy + b - y) ** 2)
                cv2.floodFill(self.mask, None, (int(xx[idx] + a), int(yy[idx] + b)), 0)
            else:
                yy, xx = np.nonzero(self.pending_mask[b:d, a:c])
                if len(xx):
                    idx = np.argmin((xx + a - x) ** 2 + (yy + b - y) ** 2)
                    remaining = self.pending_mask.copy()
                    cv2.floodFill(remaining, None, (int(xx[idx] + a), int(yy[idx] + b)), 0)
                    self.pending_mask = remaining
        else:
            color = 255 if mode == "add" else 0
            for p in coords:
                cv2.circle(self.mask, p, radius, color, -1)
            for p, q in zip(coords, coords[1:]):
                cv2.line(self.mask, p, q, color, radius * 2)
            if np.any(self.pending_mask):
                remaining = self.pending_mask.copy()
                if mode == "add":
                    remaining[self.mask > 0] = 0
                else:
                    for p in coords:
                        cv2.circle(remaining, p, radius, 0, -1)
                    for p, q in zip(coords, coords[1:]):
                        cv2.line(remaining, p, q, 0, radius * 2)
                self.pending_mask = remaining
        if mode in ("erase", "reject"):
            self.clone_strokes = [dict(stroke, mask=np.minimum(stroke["mask"], self.mask[stroke["y0"]:stroke["y1"], stroke["x0"]:stroke["x1"]])) for stroke in self.clone_strokes]
        self.changed()
        self.finish_local_edit(mode)

    def clone(self, source, points, diameter):
        """Copy original, clean donor pixels; preserve depth and all unselected data."""
        if not source or len(source) != 2 or not points or len(points) > 12000:
            raise ValueError("请先取样，再涂抹需要修补的位置。")
        data = np.asarray([source] + points, dtype=float)
        if data.shape[1] != 2 or not np.all(np.isfinite(data)):
            raise ValueError("取样坐标无效。")
        sx, sy = [int(round(v)) for v in source]
        if not (0 <= sx < self.width and 0 <= sy < self.height) or self.mask[sy, sx] or self.pending_mask[sy, sx]:
            raise ValueError("请选择红色和黄色标记以外的干净位置取样。")
        coords = [(int(np.clip(x, 0, self.width - 1)), int(np.clip(y, 0, self.height - 1))) for x, y in points]
        dx, dy = sx - coords[0][0], sy - coords[0][1]
        radius = max(1, min(250, round(float(diameter) / 2)))
        x0 = max(0, min(p[0] for p in coords) - radius)
        y0 = max(0, min(p[1] for p in coords) - radius)
        x1 = min(self.width, max(p[0] for p in coords) + radius + 1)
        y1 = min(self.height, max(p[1] for p in coords) + radius + 1)
        local = np.zeros((y1 - y0, x1 - x0), np.uint8)
        for x, y in coords:
            cv2.circle(local, (x - x0, y - y0), radius, 255, -1)
        for p, q in zip(coords, coords[1:]):
            cv2.line(local, (p[0] - x0, p[1] - y0), (q[0] - x0, q[1] - y0), 255, radius * 2)
        yy, xx = np.nonzero(local)
        ax, ay = xx + x0 + dx, yy + y0 + dy
        valid = (ax >= 0) & (ax < self.width) & (ay >= 0) & (ay < self.height)
        clean = np.zeros_like(valid)
        clean[valid] = (self.mask[ay[valid], ax[valid]] == 0) & (self.pending_mask[ay[valid], ax[valid]] == 0)
        local[yy[~clean], xx[~clean]] = 0
        if not local.any():
            raise ValueError("取样范围包含灰尘或超出照片，请换一个干净位置。")
        self.checkpoint()
        self.mask[y0:y1, x0:x1] = np.maximum(self.mask[y0:y1, x0:x1], local)
        remaining = self.pending_mask.copy()
        remaining[self.mask > 0] = 0
        self.pending_mask = remaining
        self.clone_strokes.append(dict(x0=x0, y0=y0, x1=x1, y1=y1, dx=dx, dy=dy, mask=local))
        self.changed()
        touched = np.zeros_like(self.mask)
        touched[y0:y1, x0:x1] = local
        self.finish_local_edit("clone", touched, self.clone_strokes[-1])

    def keep_regions(self, points):
        if not points or len(points) > 12000:
            raise ValueError("请选择要保留的位置。")
        coords = np.asarray(points, dtype=float)
        if coords.ndim != 2 or coords.shape[1] != 2 or not np.all(np.isfinite(coords)):
            raise ValueError("保留位置无效。")
        self.checkpoint()
        remaining = self.pending_mask.copy()
        for px, py in coords:
            x, y = int(np.clip(round(px), 0, self.width - 1)), int(np.clip(round(py), 0, self.height - 1))
            if remaining[y, x]:
                cv2.floodFill(remaining, None, (x, y), 0)
            elif self.mask[y, x]:
                cv2.floodFill(self.mask, None, (x, y), 0)
        self.pending_mask = remaining
        self.clone_strokes = [dict(s, mask=np.minimum(s["mask"], self.mask[s["y0"]:s["y1"], s["x0"]:s["x1"]])) for s in self.clone_strokes]
        self.changed()
        self.finish_local_edit("restore")

    def undo(self):
        if self.history:
            previous_revision = self.revision
            saved = self.history.pop()
            self.changed()
            for key in ('mask', 'clone_strokes', 'pending_mask', 'detection_report', 'result', 'heal_report', 'edit_report'):
                setattr(self, key, saved[key])
            self.healed_revision = self.revision
            if saved['bounds'] is not None:
                self.preview_patch = dict(from_revision=previous_revision, bounds=saved['bounds'])

    def clear(self):
        self.checkpoint()
        self.mask.fill(0)
        self.pending_mask = np.zeros_like(self.mask)
        self.detection_report = None
        self.clone_strokes = []
        self.changed()
        self.finish_local_edit("clear")

    def heal(self):
        if self.result is None or self.healed_revision != self.revision:
            auto_mask = self.mask.copy()
            for stroke in self.clone_strokes:
                view = auto_mask[stroke["y0"]:stroke["y1"], stroke["x0"]:stroke["x1"]]
                view[stroke["mask"] > 0] = 0
            self.heal_report = {}
            self.result = self.repair(auto_mask, self.heal_report)
            for stroke in self.clone_strokes:
                yy, xx = np.nonzero(stroke["mask"])
                xx, yy = xx + stroke["x0"], yy + stroke["y0"]
                active = self.mask[yy, xx] > 0
                xx, yy = xx[active], yy[active]
                self.result[yy, xx] = self.original[yy + stroke["dy"], xx + stroke["dx"]]
                if self.associated_alpha:
                    donor_alpha = self.alpha[yy + stroke["dy"], xx + stroke["dx"]].astype(np.float32)
                    values = self.result[yy, xx, :-1].astype(np.float32) * self.alpha[yy, xx, None] / np.maximum(donor_alpha[:, None], 1)
                    self.result[yy, xx, :-1] = np.minimum(np.round(values), self.alpha[yy, xx, None]).astype(self.original.dtype)
            if self.alpha is not None:
                self.result[:, :, -1] = self.alpha
            self.healed_revision = self.revision
        return self.result

    def export(self, fmt):
        result = self.heal()
        stream = io.BytesIO()
        if fmt == "tiff":
            tags = [(34675, "B", len(self.icc), self.icc, False)] if self.icc else []
            tifffile.imwrite(stream, result, photometric="minisblack" if self.gray else "rgb",
                             planarconfig="contig" if result.ndim == 3 else None,
                             extrasamples=["ASSOCALPHA" if self.associated_alpha else "UNASSALPHA"] if self.alpha is not None else None,
                             metadata=None, extratags=tags, resolution=self.dpi, resolutionunit="INCH")
            return stream.getvalue(), "image/tiff", ".tif"
        if fmt not in ("png", "jpeg"):
            raise ValueError("不支持的导出格式。")
        if fmt == "png" and self.depth == 16:
            raise ValueError("16 位原稿请导出 TIFF，以保留位深。")
        result = self.straight_pixels(result)
        pixels = (result // 257).astype(np.uint8) if self.depth == 16 else result
        im = Image.fromarray(pixels)
        if fmt == "jpeg" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB" if not self.gray else "L")
        kwargs = dict(dpi=self.dpi)
        if self.icc:
            kwargs["icc_profile"] = self.icc
        if fmt == "jpeg":
            kwargs.update(quality=98, subsampling=0)
        im.save(stream, format="JPEG" if fmt == "jpeg" else "PNG", **kwargs)
        return stream.getvalue(), "image/jpeg" if fmt == "jpeg" else "image/png", ".jpg" if fmt == "jpeg" else ".png"


def orient_array(a, orientation):
    if orientation == 2:
        return a[:, ::-1]
    if orientation == 3:
        return a[::-1, ::-1]
    if orientation == 4:
        return a[::-1]
    if orientation == 5:
        return np.swapaxes(a, 0, 1)
    if orientation == 6:
        return np.rot90(a, 3)
    if orientation == 7:
        return np.swapaxes(a, 0, 1)[::-1, ::-1]
    if orientation == 8:
        return np.rot90(a, 1)
    return a


def _thin_skeleton(mask):
    """Zhang-Suen thinning on a single small candidate, for branch rejection."""
    a = np.pad(mask.astype(bool), 1)
    for _ in range(60):
        changed = False
        for step in (0, 1):
            p = a[1:-1, 1:-1]
            ns = [a[:-2, 1:-1], a[:-2, 2:], a[1:-1, 2:], a[2:, 2:],
                  a[2:, 1:-1], a[2:, :-2], a[1:-1, :-2], a[:-2, :-2]]
            neighbors = sum(v.astype(np.uint8) for v in ns)
            transitions = sum((~ns[i] & ns[(i + 1) % 8]).astype(np.uint8) for i in range(8))
            constraint = (~(ns[0] & ns[2] & ns[4]) & ~(ns[2] & ns[4] & ns[6])) if step == 0 else (~(ns[0] & ns[2] & ns[6]) & ~(ns[0] & ns[4] & ns[6]))
            remove = p & (neighbors >= 2) & (neighbors <= 6) & (transitions == 1) & constraint
            if remove.any():
                p[remove] = False
                changed = True
        if not changed:
            break
    return a[1:-1, 1:-1].astype(np.uint8)


def _frame_exclusion(gray):
    height, width = gray.shape
    dark = (gray < 24).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(dark, 8)
    ids = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
    ids = ids[(ids > 0) & (stats[ids, cv2.CC_STAT_AREA] > 64)]
    excluded = cv2.dilate(np.isin(labels, ids).astype(np.uint8), np.ones((17, 17), np.uint8))
    margin = max(2, round(min(height, width) * .015))
    excluded[:margin] = 1
    excluded[-margin:] = 1
    excluded[:, :margin] = 1
    excluded[:, -margin:] = 1
    return excluded


def _straight_structure_band(comp):
    """A long, narrow, nearly straight candidate is scene structure, not dust.

    Platform edges, safety lines, rails, wires and window bars are often diagonal,
    so the axis-aligned box is mostly empty and the filled-box hold never sees them.
    A wide median also erases a thin line, which is why the edge gate misses it.
    Measure length and straightness along the band itself. Sensitivity and the
    existing split limits are unchanged. Curved strands stay selectable.
    """
    ys, xs = np.nonzero(comp)
    count = int(xs.size)
    if count < 48:
        return False
    points = np.column_stack((xs, ys)).astype(np.float32)
    vx, vy, xm, ym = cv2.fitLine(points, cv2.DIST_L2, 0, .01, .01).ravel()
    dist = np.abs((xs - xm) * vy - (ys - ym) * vx)
    tangent = xs * vx + ys * vy
    fit_len = float(tangent.max() - tangent.min()) + 1.
    if fit_len < 80:
        return False
    thick = count / fit_len
    if thick < .7 or thick > 24 or fit_len / thick < 8:
        return False
    # A filled straight band sits about thick/4 from its centerline. Extra
    # deviation is bend. Window bars are a little uneven; arcs and blobs are not.
    if float(np.mean(dist)) - thick / 4 > 3 or float(np.percentile(dist, 95)) > max(10., thick * 1.25):
        return False
    bins = max(8, int(fit_len))
    index = np.clip(((tangent - float(tangent.min())) / fit_len * (bins - 1)).astype(np.int32), 0, bins - 1)
    return float(np.count_nonzero(np.bincount(index, minlength=bins))) / bins >= .8


def split_automatic_candidates(rgb, proposal, level="gentle", polarity="light"):
    """Keep pixel/structure conflicts as original pixels, never an AI verdict."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(proposal, 8)
    take = np.zeros(n, np.uint8)
    hold = np.zeros(n, np.uint8)
    gray_all = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = proposal.shape
    limits = {"auto": (5., .45, 15., 1000), "gentle": (1.3, .10, 4., 160), "standard": (1.7, .15, 6., 240), "more": (2., .20, 8., 400)}[level]
    edge_limit, texture_limit, variation_limit, length_limit = limits
    for k in range(1, n):
        x, y, w, h, area = [int(v) for v in stats[k]]
        x0, y0 = max(0, x - 40), max(0, y - 40)
        x1, y1 = min(width, x + w + 40), min(height, y + h + 40)
        gray = gray_all[y0:y1, x0:x1]
        ref = cv2.GaussianBlur(cv2.medianBlur(gray, 31).astype(np.float32), (0, 0), 1.4)
        ix, iy = x - x0, y - y0
        region = np.s_[max(0, iy - 6):iy + h + 6, max(0, ix - 6):ix + w + 6]
        gradient = cv2.magnitude(cv2.Sobel(ref, cv2.CV_32F, 1, 0) / 8, cv2.Sobel(ref, cv2.CV_32F, 0, 1) / 8)[region]
        scene = cv2.medianBlur(gray, 7).astype(np.float32)
        band = np.abs(scene - cv2.GaussianBlur(scene, (0, 0), 4))
        background = ref[iy:iy + h, ix:ix + w]
        delta = (gray.astype(np.float32) - ref)[iy:iy + h, ix:ix + w]
        bright, dark = float(delta.max()), float(-delta.min())
        # 57px is the shorter side of an 80px diagonal, so a slanted safety line
        # still reaches the straight-band check. Limits above are intentionally
        # the same numbers as before.
        unsafe = (max(w, h) > length_limit or np.mean(gradient > edge_limit) > .02 or
                  np.mean(band > 3) > texture_limit or np.std(background) > variation_limit or
                  (max(w, h) > (80 if level == "auto" else 16) and max(w, h) / max(1, min(w, h)) > (8 if level == "auto" else 2.5) and area / (w * h) > .40) or
                  (polarity == "light" and dark > max(12, bright * 1.5)) or
                  (polarity == "dark" and bright > max(12, dark * 1.5)) or
                  (max(w, h) >= 57 and _straight_structure_band(labels[y:y + h, x:x + w] == k)))
        if unsafe:
            hold[k] = 255
        else:
            take[k] = 255
    return take[labels], hold[labels], dict(candidates=n - 1, selected=int(np.count_nonzero(take)), kept=int(np.count_nonzero(hold)))


def detect_dust(rgb, sensitivity=50, size=30, hairs=True, protect=True, polarity="both"):
    """Strong dust plus coherent faint defects; dense scene texture is protected.

    Do not erase candidate pixels before measuring background texture: doing so
    makes a tiled roof look flat. Texture occupancy over a wider neighborhood
    distinguishes repeating highlights from one isolated hair or dust blob.
    """
    sensitivity = float(np.clip(sensitivity, 1, 100))
    size = int(np.clip(size, 4, 100))
    if polarity not in ("both", "light", "dark"):
        raise ValueError("未知灰尘颜色设置。")
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    extent = np.zeros_like(gray)
    scores = np.zeros(gray.shape, np.float32)
    scene = np.zeros_like(gray)
    # 96px halo covers the wider texture neighborhood and avoids tile seams.
    for y in range(0, height, 1024):
        for x in range(0, width, 1024):
            y0, y1 = max(0, y - 96), min(height, y + 1120)
            x0, x1 = max(0, x - 96), min(width, x + 1120)
            patch = gray[y0:y1, x0:x1]
            f = patch.astype(np.float32)
            smooth = cv2.GaussianBlur(f, (0, 0), 1.4)
            ref = cv2.GaussianBlur(cv2.medianBlur(patch, 31).astype(np.float32), (0, 0), 1.4)
            delta = smooth - ref
            residual = np.maximum(delta, 0) if polarity == "light" else np.maximum(-delta, 0) if polarity == "dark" else np.abs(delta)
            noise = cv2.medianBlur(np.clip(np.abs(f - cv2.medianBlur(patch, 5)), 0, 255).astype(np.uint8), 9).astype(np.float32)
            score = residual / (2 + (100 - sensitivity) * .02 + noise * .55)
            bg = cv2.GaussianBlur(cv2.medianBlur(patch, 7).astype(np.float32), (0, 0), .8)
            band = np.abs(bg - cv2.GaussianBlur(bg, (0, 0), 4))
            detail = cv2.GaussianBlur(band, (0, 0), 15)
            occupancy = cv2.boxFilter((band > 2).astype(np.float32), -1, (81, 81))
            gradient = cv2.magnitude(cv2.Sobel(ref, cv2.CV_32F, 1, 0) / 8, cv2.Sobel(ref, cv2.CV_32F, 0, 1) / 8)
            boundaries = cv2.dilate((gradient > 5).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
            excluded = ((detail > 1) & (occupancy > .40)) | boundaries
            sy, sx = y - y0, x - x0
            hh, ww = min(1024, height - y), min(1024, width - x)
            source = np.s_[sy:sy + hh, sx:sx + ww]
            target = np.s_[y:y + hh, x:x + ww]
            extent[target] = ((score > .70) & ~excluded)[source].astype(np.uint8) * 255
            scores[target] = score[source]
            scene[target] = excluded[source].astype(np.uint8)
    if protect:
        scene |= _frame_exclusion(gray)
        extent[scene > 0] = 0
    extent = cv2.morphologyEx(extent, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    extent[scene > 0] = 0
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(extent, 8)
    peaks = np.zeros(n, np.float32)
    np.maximum.at(peaks, labels.ravel(), scores.ravel())
    keep = np.zeros(n, np.uint8)
    centers = np.zeros(gray.shape, np.float32)
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if peaks[k] < 1.6 or area < 8 or area > size * size * 8:
            continue
        local = (labels[y:y + h, x:x + w] == k).astype(np.uint8)
        mean_score = float(np.mean(scores[y:y + h, x:x + w][local > 0]))
        if mean_score < 1.3:
            continue
        length, density = max(w, h), area / (w * h)
        radius = cv2.distanceTransform(local, cv2.DIST_L2, 3).max()
        if length <= size * 2 and density > .1 and area < size * size * 2 and (hairs or max(w, h) / max(1, min(w, h)) < 3.8):
            keep[k] = 255
        elif hairs and length <= min(1000, size * 30) and area / length < 18 and radius < 9:
            yy, xx = np.nonzero(local)
            points = np.column_stack((xx, yy)).astype(np.float32)
            vx, vy, xm, ym = cv2.fitLine(points, cv2.DIST_L2, 0, .01, .01).ravel()
            bend = np.mean(np.abs((xx - xm) * vy - (yy - ym) * vx))
            skeleton = _thin_skeleton(local)
            neighbors = cv2.filter2D(skeleton, -1, np.ones((3, 3), np.uint8)) - skeleton
            endpoints = np.count_nonzero((neighbors == 1) & (skeleton > 0))
            branches = ((neighbors > 2) & (skeleton > 0)).astype(np.uint8)
            nbranches = cv2.connectedComponents(branches, 8)[0] - 1
            coherent = endpoints <= 6 and (nbranches <= 4 or (endpoints <= 4 and mean_score > 1.6))
            if coherent and (length < 65 or bend > 1.5):
                keep[k] = 255
        if keep[k]:
            cx, cy = centroids[k]
            centers[int(cy), int(cx)] = 1
    crowd = cv2.boxFilter(centers, -1, (121, 121), normalize=False)
    mask = keep[labels]
    mask[crowd > 7] = 0
    strong = _detect_strong_dust(rgb, sensitivity, size, hairs, protect, polarity)
    strong[scene > 0] = 0
    mask = np.maximum(mask, strong)
    # Separate a bright, thicker speck attached to a thin line, without selecting
    # the whole line (e.g. a flake immediately next to an antenna or scratch).
    cores = cv2.morphologyEx(((scores > 2) & (scene == 0)).astype(np.uint8) * 255, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    nc, clabels, cstats, _ = cv2.connectedComponentsWithStats(cores, 8)
    ckeep = np.zeros(nc, np.uint8)
    for k in range(1, nc):
        x, y, w, h, area = cstats[k]
        if area >= 4 and max(w, h) <= size * 2 and min(w, h) <= size and area / (w * h) > .25:
            ckeep[k] = 255
        elif max(w, h) > size * 2 and min(w, h) < size * 2:
            local = (clabels[y:y + h, x:x + w] == k).astype(np.uint8)
            distance = cv2.distanceTransform(local, cv2.DIST_L2, 3)
            islands = (distance > max(3, float(distance.max()) * .65)).astype(np.uint8)
            ni, ilabels, istats, _ = cv2.connectedComponentsWithStats(islands, 8)
            ikeep = np.zeros(ni, np.uint8)
            for j in range(1, ni):
                _, _, iw, ih, ia = istats[j]
                if ia >= 3 and max(iw, ih) <= size * 2:
                    ikeep[j] = 255
            recovered = cv2.dilate(ikeep[ilabels], cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
            mask[y:y + h, x:x + w] = np.maximum(mask[y:y + h, x:x + w], recovered)
    mask = np.maximum(mask, cv2.dilate(ckeep[clabels], cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))))
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    mask[scene > 0] = 0
    return mask


def _detect_strong_dust(rgb, sensitivity=50, size=30, hairs=False, protect=True, polarity="both"):
    """Multiscale residuals with texture/edge penalties, native-resolution tiles."""
    sensitivity = float(np.clip(sensitivity, 1, 100))
    size = int(np.clip(size, 4, 100))
    if polarity not in ("both", "light", "dark"):
        raise ValueError("未知灰尘颜色设置。")
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    # No global resize: single-pixel marks stay visible.
    candidate = np.zeros_like(gray)
    strength = np.zeros_like(gray, dtype=np.float32)
    threshold = 5 + (100 - sensitivity) * .18
    for y in range(0, height, 1024):
        for x in range(0, width, 1024):
            y0, y1 = max(0, y - 32), min(height, y + 1056)
            x0, x1 = max(0, x - 32), min(width, x + 1056)
            patch = gray[y0:y1, x0:x1]
            f = patch.astype(np.float32)
            reference = cv2.medianBlur(patch, 21).astype(np.float32)
            fine = cv2.medianBlur(patch, 5).astype(np.float32)
            delta = f - reference
            fine_delta = f - fine
            residual = np.maximum(delta, 0) if polarity == "light" else np.maximum(-delta, 0) if polarity == "dark" else np.abs(delta)
            # Median of residuals estimates local grain without reacting to isolated dust.
            noise = cv2.medianBlur(np.clip(np.abs(fine_delta), 0, 255).astype(np.uint8), 9).astype(np.float32)
            smooth = cv2.GaussianBlur(reference, (0, 0), 1.5)
            gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3) / 8
            gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3) / 8
            edges = cv2.magnitude(gx, gy)
            gate = threshold + noise * 3 + edges * 1.8
            score = residual / np.maximum(gate, 1)
            # Texture and scene boundaries are intentionally left for manual marks.
            # The median reference suppresses dust itself so isolated specks do
            # not disqualify their own otherwise smooth background.
            background = cv2.medianBlur(patch, 7).astype(np.float32)
            background = np.where(score > 1, reference, background)
            mean = cv2.GaussianBlur(background, (0, 0), 7)
            variance = cv2.GaussianBlur(background * background, (0, 0), 7) - mean * mean
            texture = np.sqrt(np.maximum(variance, 0))
            boundaries = cv2.dilate((edges > 2.5 + sensitivity * .055).astype(np.uint8), np.ones((7, 7), np.uint8))
            marks = ((score > 1) & (texture < 4 + sensitivity * .08) & (boundaries == 0)).astype(np.uint8) * 255
            sy, sx = y - y0, x - x0
            hh, ww = min(1024, height - y), min(1024, width - x)
            candidate[y:y + hh, x:x + ww] = marks[sy:sy + hh, sx:sx + ww]
            strength[y:y + hh, x:x + ww] = score[sy:sy + hh, sx:sx + ww]
    if protect:
        # Exclude dark film surround and the picture/frame seam, plus outer 1.5%.
        dark = (gray < 24).astype(np.uint8)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(dark, 8)
        border_ids = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
        border_ids = border_ids[(border_ids > 0) & (stats[border_ids, cv2.CC_STAT_AREA] > 64)]
        excluded = np.isin(labels, border_ids).astype(np.uint8)
        excluded = cv2.dilate(excluded, np.ones((17, 17), np.uint8))
        margin = max(2, round(min(height, width) * .015))
        excluded[:margin] = 1
        excluded[-margin:] = 1
        excluded[:, :margin] = 1
        excluded[:, -margin:] = 1
        candidate[excluded > 0] = 0
    n, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, 8)
    keep = np.zeros(n, dtype=np.uint8)
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if area < 4 or area > size * size:
            continue
        aspect = max(w, h) / max(1, min(w, h))
        density = area / (w * h)
        if max(w, h) <= size and aspect < 3.8 and density > .13:
            keep[k] = 255
        elif hairs and max(w, h) <= size * 6 and area / max(w, h) < 7 and density < .60:
            # Long thin candidates remain opt-in because wires/hair can look identical.
            local = (labels[y:y + h, x:x + w] == k)
            if np.mean(strength[y:y + h, x:x + w][local]) > 1.35:
                keep[k] = 255
    mask = keep[labels]
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    if protect:
        mask[excluded > 0] = 0
    return mask


def _rgb8(pixels, gray=False):
    small = (pixels // 257).astype(np.uint8) if pixels.dtype == np.uint16 else pixels
    if small.ndim == 2 or gray:
        mono = small if small.ndim == 2 else small[:, :, 0]
        return np.repeat(mono[:, :, None], 3, axis=2)
    return np.ascontiguousarray(small[:, :, :3])


def _edge_context(patch8, mask):
    lum = cv2.cvtColor(patch8, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lum = cv2.GaussianBlur(lum, (0, 0), 1)
    gx = cv2.Sobel(lum, cv2.CV_32F, 1, 0) / 8
    gy = cv2.Sobel(lum, cv2.CV_32F, 0, 1) / 8
    magnitude = np.hypot(gx, gy)
    known = cv2.dilate(mask, np.ones((7, 7), np.uint8)) == 0
    valid = known & (magnitude > 2.5)
    if np.count_nonzero(valid) < 8:
        return False, None
    xx, yy = np.sum(gx[valid] ** 2), np.sum(gy[valid] ** 2)
    xy = np.sum(gx[valid] * gy[valid])
    coherence = np.hypot(xx - yy, 2 * xy) / (xx + yy + 1e-8)
    angles = np.mod(np.arctan2(gy[valid], gx[valid]), np.pi)
    bins = np.minimum(17, (angles * (18 / np.pi)).astype(int))
    histogram = np.bincount(bins, weights=magnitude[valid] ** 2, minlength=18)
    orientation_support = max(np.roll(histogram, -1) + histogram + np.roll(histogram, 1)) / (histogram.sum() + 1e-8)
    # Grain and random texture contain gradients in every direction; a scene
    # boundary has one or two persistent orientations.
    if coherence < .35 and orientation_support < .38:
        return False, None
    theta = .5 * np.arctan2(2 * xy, xx - yy)
    normal = np.array([np.cos(theta), np.sin(theta)])
    ys, xs = np.nonzero(valid)
    positions = xs * normal[0] + ys * normal[1]
    weights = magnitude[valid] ** 2
    order = np.argsort(positions)
    cumulative = np.cumsum(weights[order])
    median = positions[order[np.searchsorted(cumulative, cumulative[-1] * .5)]]
    near = np.abs(positions - median) < 4.5
    line_support = np.sum(weights[near]) / np.sum(weights)
    tangent = np.array([-normal[1], normal[0]], np.float32)
    return True, tangent if coherence > .80 and line_support > .60 else None


def _fill_along_edge(patch, all_mask, hole, tangent):
    """Interpolate between clean endpoints along the edge, across the hole."""
    height, width = hole.shape
    ys, xs = np.nonzero(hole)
    coords = np.column_stack((xs, ys)).astype(np.float32)
    safe = cv2.dilate(all_mask, np.ones((3, 3), np.uint8)) == 0
    endpoints, distances = [], []
    for sign in (-1, 1):
        found = np.zeros(len(xs), bool)
        end = coords.copy()
        distance = np.zeros(len(xs), np.float32)
        for radius in range(1, min(160, max(height, width)) + 1):
            xy = coords + tangent * (radius * sign)
            ix, iy = np.round(xy[:, 0]).astype(int), np.round(xy[:, 1]).astype(int)
            inside = (ix >= 0) & (ix < width) & (iy >= 0) & (iy < height)
            choose = np.zeros(len(xs), bool)
            choose[inside] = safe[iy[inside], ix[inside]]
            choose &= ~found
            end[choose] = xy[choose]
            distance[choose] = radius
            found |= choose
            if found.all():
                break
        if not found.all():
            return None
        endpoints.append(end)
        distances.append(distance)
    samples = [cv2.remap(patch.astype(np.float32), end[:, 0][None, :], end[:, 1][None, :], cv2.INTER_LINEAR)[0] for end in endpoints]
    weight = distances[1] / (distances[0] + distances[1])
    if patch.ndim == 3:
        weight = weight[:, None]
    values = samples[0] * weight + samples[1] * (1 - weight)
    out = patch.copy()
    out[ys, xs] = np.clip(np.round(values), 0, np.iinfo(patch.dtype).max).astype(patch.dtype)
    return out


def _match_clean_patch(original, full_mask, bounds, gray=False):
    x0, y0, x1, y1 = bounds
    target = original[y0:y1, x0:x1]
    if max(target.shape[:2]) > 150 or target.shape[0] * target.shape[1] > 10000:
        return None
    known = (cv2.dilate(full_mask[y0:y1, x0:x1], np.ones((3, 3), np.uint8)) == 0).astype(np.float32)
    if known.sum() < 30:
        return None
    height, width = full_mask.shape
    reach = 96
    sx0, sy0 = max(0, x0 - reach), max(0, y0 - reach)
    sx1, sy1 = min(width, x1 + reach), min(height, y1 + reach)
    source = _rgb8(original[sy0:sy1, sx0:sx1], gray).astype(np.float32)
    template = _rgb8(target, gray).astype(np.float32)
    # Expand weighted squared error into two unmasked correlations. OpenCV's
    # masked three-channel matcher repeats expensive normalisation work for
    # every speck; the expression below uses the same RGB error and threshold.
    weights = known[:, :, None]
    squared_source = np.sum(source * source, axis=2)
    cost = cv2.matchTemplate(squared_source, known, cv2.TM_CCORR)
    cost -= 2 * cv2.matchTemplate(source, template * weights, cv2.TM_CCORR)
    cost += np.sum(template * template * weights)
    cost = np.maximum(cost, 0) / (known.sum() * 3)
    dirty = (full_mask[sy0:sy1, sx0:sx1] > 0).astype(np.float32)
    integral = cv2.integral(dirty)
    kh, kw = known.shape
    contaminated = (integral[kh:, kw:] - integral[:-kh, kw:] - integral[kh:, :-kw] + integral[:-kh, :-kw]) > .5
    cost[contaminated] = np.inf
    cost[~np.isfinite(cost)] = np.inf
    yy, xx = np.indices(cost.shape)
    distance = (xx + sx0 - x0) ** 2 + (yy + sy0 - y0) ** 2
    ranked = cost + distance * .00015
    by, bx = np.unravel_index(np.argmin(ranked), ranked.shape)
    if not np.isfinite(cost[by, bx]) or cost[by, bx] > 49:
        return None
    bx, by = bx + sx0, by + sy0
    return original[by:by + target.shape[0], bx:bx + target.shape[1]].copy()


def heal_mask(original, mask, gray=False, report=None, manual=False):
    """Use nearby fill on flat areas; protect scene edges and flag uncertainty."""
    summary = dict(local=0, edge=0, donor=0, pending=[])
    result = _heal_local(original, mask, gray, enforce_limit=not manual)
    if np.any(mask):
        n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        height, width = mask.shape
        for k in range(1, n):
            x, y, w, h, _ = stats[k]
            pad = max(16, min(40, round(max(w, h) * .3)))
            x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(width, x + w + pad), min(height, y + h + pad)
            patch = original[y0:y1, x0:x1]
            all_mask = mask[y0:y1, x0:x1]
            hole = (labels[y0:y1, x0:x1] == k).astype(np.uint8) * 255
            has_edge, tangent = _edge_context(_rgb8(patch, gray), all_mask)
            if not has_edge:
                summary["local"] += 1
                continue
            candidate = _fill_along_edge(patch, all_mask, hole, tangent) if tangent is not None else None
            if candidate is not None:
                summary["edge"] += 1
            else:
                candidate = _match_clean_patch(original, mask, (x0, y0, x1, y1), gray)
                if candidate is not None:
                    summary["donor"] += 1
            if candidate is None:
                # Keep original geometry when there is no reliable reconstruction.
                if manual:
                    # The user has explicitly identified this as dust. Keep the
                    # local fill instead of silently restoring the original mark.
                    summary["manual_fallback"] = summary.get("manual_fallback", 0) + 1
                    continue
                candidate = patch
                summary["pending"].append([int(round(v)) for v in centroids[k]])
            selected = hole > 0
            result[y0:y1, x0:x1][selected] = candidate[selected]
    if original.ndim == 3 and original.shape[2] in (2, 4):
        result[:, :, -1] = original[:, :, -1]
    if report is not None:
        report.update(summary)
    return result


def _heal_local(original, mask, gray=False, enforce_limit=True):
    """Heal only masked pixels at original depth; unchanged pixels are bit-exact."""
    result = original.copy()
    if not np.any(mask):
        return result
    height, width = mask.shape
    selected_total = np.count_nonzero(mask)
    if enforce_limit and selected_total / mask.size > .15:
        raise ValueError("选区超过照片的 15%，请先减少选区；此工具适合小灰尘和细丝。")
    for y in range(0, height, 768):
        for x in range(0, width, 768):
            hh, ww = min(768, height - y), min(768, width - x)
            central_mask = mask[y:y + hh, x:x + ww] > 0
            if not central_mask.any():
                continue
            y0, y1 = max(0, y - 48), min(height, y + hh + 48)
            x0, x1 = max(0, x - 48), min(width, x + ww + 48)
            local_mask = mask[y0:y1, x0:x1]
            patch = original[y0:y1, x0:x1]
            # A surrounding 48px halo avoids seams where tiny defects cross tiles.
            if original.ndim == 2:
                healed = cv2.inpaint(patch, local_mask, 4, cv2.INPAINT_NS)
            else:
                healed = patch.copy()
                channels = 1 if gray else 3
                for ch in range(channels):
                    healed[:, :, ch] = cv2.inpaint(np.ascontiguousarray(patch[:, :, ch]), local_mask, 4, cv2.INPAINT_NS)
            sy, sx = y - y0, x - x0
            target = result[y:y + hh, x:x + ww]
            source = healed[sy:sy + hh, sx:sx + ww]
            target[central_mask] = source[central_mask]
    return result


def png_bytes(array):
    out = io.BytesIO()
    Image.fromarray(array).save(out, format="PNG", compress_level=2)
    return out.getvalue()


def downscale(array, max_edge):
    """Shrink so the long edge is at most max_edge. Area sampling keeps thin marks."""
    height, width = array.shape[:2]
    long_edge = max(height, width)
    if not max_edge or long_edge <= max_edge:
        return array
    scale = max_edge / long_edge
    size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return cv2.resize(array, size, interpolation=cv2.INTER_AREA)


def lossy_preview_bytes(array, detailed=False):
    """Browser preview. Photographs are JPEG; alpha overlays are WebP.

    Rect updates use a higher quality so a patched comparison does not smear.
    Full-frame previews favor size and encode time. Neither path is an export.
    """
    array = np.ascontiguousarray(array)
    quality = 93 if detailed else 80
    if array.ndim == 3 and array.shape[2] == 4:
        bgra = cv2.cvtColor(array, cv2.COLOR_RGBA2BGRA)
        ok, encoded = cv2.imencode(".webp", bgra, [int(cv2.IMWRITE_WEBP_QUALITY), quality])
        mime = "image/webp"
    elif array.ndim == 2 or (array.ndim == 3 and array.shape[2] == 3):
        source = cv2.cvtColor(array, cv2.COLOR_RGB2BGR) if array.ndim == 3 else array
        sampling = cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444 if detailed else cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420
        ok, encoded = cv2.imencode(".jpg", source, [
            int(cv2.IMWRITE_JPEG_QUALITY), quality,
            int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR), sampling])
        mime = "image/jpeg"
    else:
        raise ValueError("无法编码此预览。")
    if not ok:
        raise ValueError("预览编码失败。")
    return encoded.tobytes(), mime
