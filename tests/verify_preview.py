"""Lossy progressive previews. Export stays lossless and detection stays put."""
import hashlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import cv2
import numpy as np
import tifffile
from PIL import Image, ImageCms

import engine
import server
from engine import (
    Photo, _straight_structure_band, detect_dust, png_bytes, scale_kernel,
    scale_length, scaled_dust_size,
)


def png(array):
    out = io.BytesIO()
    Image.fromarray(array).save(out, format="PNG")
    return out.getvalue()


def tiff_bytes(array, icc=None, dpi=(300, 300)):
    stream = io.BytesIO()
    tags = [(34675, "B", len(icc), icc, False)] if icc else []
    tifffile.imwrite(stream, array, photometric="rgb" if array.ndim == 3 else "minisblack",
                     resolution=dpi, resolutionunit="INCH", extratags=tags)
    return stream.getvalue()


class PreviewContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.service.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.service.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.service.shutdown()
        cls.service.server_close()
        cls.thread.join()

    def setUp(self):
        server.SESSIONS.clear()
        server.ACTIVE_ID = None

    def post(self, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body or {}).encode()
        request = urllib.request.Request(self.base + path, data=data, headers={"X-FilmDust-Token": server.TOKEN, "Origin": self.base})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def upload(self, raw, name="scan.png"):
        status, body, _ = self.post("/api/upload?name=" + name, raw=raw)
        self.assertEqual(status, 200)
        return json.loads(body)

    def get_image(self, ident, kind, revision, rect=None, max_edge=None):
        url = f"{self.base}/api/image?id={ident}&kind={kind}&v={revision}"
        if rect is not None:
            url += "&rect=" + rect
        if max_edge is not None:
            url += "&max=" + str(max_edge)
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(), response.headers["Content-Type"]

    def test_export_stays_lossless_after_lossy_preview(self):
        rng = np.random.default_rng(9)
        original = rng.integers(1000, 60000, (96, 120, 3), dtype=np.uint16)
        original[40:48, 50:58] = 65000
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        uploaded = self.upload(tiff_bytes(original, icc, (240, 240)), "scan.tif")
        status, body, _ = self.post("/api/edit", {"id": uploaded["id"], "mode": "add", "points": [[54, 44]], "diameter": 8, "revision": 0})
        self.assertEqual(status, 200)
        info = json.loads(body)
        preview_status, preview, mime = self.get_image(uploaded["id"], "result", info["revision"], max_edge=64)
        self.assertEqual(preview_status, 200)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(preview.startswith(b"\xff\xd8"))
        self.assertLessEqual(max(Image.open(io.BytesIO(preview)).size), 64)
        status, exported, headers = self.post("/api/export", {"id": uploaded["id"], "revision": info["revision"], "format": "tiff"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/tiff")
        photo = server.SESSIONS[uploaded["id"]]
        with tifffile.TiffFile(io.BytesIO(exported)) as tf:
            page = tf.pages[0]
            self.assertEqual(page.tags["XResolution"].value, (240, 1))
            self.assertEqual(page.tags["YResolution"].value, (240, 1))
            self.assertEqual(bytes(page.tags["InterColorProfile"].value), icc)
            decoded = page.asarray()
        self.assertEqual(decoded.dtype, np.uint16)
        self.assertEqual(decoded.shape, original.shape)
        np.testing.assert_array_equal(decoded, photo.heal())
        np.testing.assert_array_equal(decoded[photo.mask == 0], original[photo.mask == 0])
        self.assertTrue(np.any(decoded[photo.mask > 0] != original[photo.mask > 0]))
        self.assertEqual(self.post("/api/export", {"id": uploaded["id"], "revision": 0})[0], 400)

    def test_full_frame_preview_is_lossy_and_overlay_is_webp(self):
        rng = np.random.default_rng(4)
        original = rng.integers(0, 256, (180, 240, 3), dtype=np.uint8)
        uploaded = self.upload(png(original), "noise.png")
        photo = server.SESSIONS[uploaded["id"]]
        _, preview, mime = self.get_image(uploaded["id"], "original", 0)
        self.assertEqual(mime, "image/jpeg")
        self.assertFalse(preview.startswith(b"\x89PNG"))
        decoded = np.asarray(Image.open(io.BytesIO(preview)).convert("RGB"))
        self.assertEqual(decoded.shape, photo.preview.shape)
        self.assertFalse(np.array_equal(decoded, photo.preview))
        self.assertLess(len(preview), len(png_bytes(photo.preview)))
        photo.edit("add", [[120, 90]], 6)
        _, overlay, overlay_mime = self.get_image(uploaded["id"], "overlay", photo.revision)
        self.assertEqual(overlay_mime, "image/webp")
        self.assertTrue(overlay.startswith(b"RIFF") and b"WEBP" in overlay[:16])
        decoded_overlay = np.asarray(Image.open(io.BytesIO(overlay)))
        self.assertEqual(decoded_overlay.shape[2], 4)
        self.assertGreater(int(decoded_overlay[:, :, 3].max()), 0)
        _, mask, mask_mime = self.get_image(uploaded["id"], "mask", photo.revision)
        self.assertEqual(mask_mime, "image/jpeg")
        self.assertNotEqual(mask[:8], b"\x89PNG\r\n\x1a\n")

    def test_quick_edge_shrinks_and_stale_revision_and_rect_are_rejected(self):
        original = np.full((360, 480, 3), 140, np.uint8)
        original[40:70, 50:80] = 250
        uploaded = self.upload(png(original), "frame.png")
        _, quick, _ = self.get_image(uploaded["id"], "original", 0, max_edge=128)
        quick_image = Image.open(io.BytesIO(quick))
        self.assertEqual(max(quick_image.size), 128)
        with self.assertRaises(urllib.error.HTTPError):
            self.get_image(uploaded["id"], "result", 1)
        with self.assertRaises(urllib.error.HTTPError):
            self.get_image(uploaded["id"], "overlay", 5, rect="0,0,20,20")
        with self.assertRaises(urllib.error.HTTPError):
            self.get_image(uploaded["id"], "result", 0, rect="-1,0,10,10")
        with self.assertRaises(urllib.error.HTTPError):
            self.get_image(uploaded["id"], "result", 0, rect="0,0,9999,10")
        with self.assertRaises(urllib.error.HTTPError):
            self.get_image(uploaded["id"], "original", 0, max_edge=8)
        with self.assertRaises(urllib.error.HTTPError):
            self.get_image(uploaded["id"], "original", 0, max_edge="nope")

    def test_original_preview_during_detect_does_not_change_auto_result(self):
        rng = np.random.default_rng(11)
        original = rng.integers(80, 160, (220, 260, 3), dtype=np.uint8)
        cv2_speck = original.copy()
        cv2_speck[80:86, 90:96] = 250
        raw = png(cv2_speck)
        control = Photo(raw, "control.png")
        control.process("auto")
        uploaded = self.upload(raw, "during.png")
        entered = threading.Event()
        release = threading.Event()
        real = engine.detect_dust

        def wrapped(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("原图预览没有在识别期间返回。")
            return real(*args, **kwargs)

        def run_process():
            self.process_status = self.post("/api/process", {"id": uploaded["id"], "level": "auto"})[0]

        with patch("engine.detect_dust", side_effect=wrapped):
            worker = threading.Thread(target=run_process)
            worker.start()
            try:
                self.assertTrue(entered.wait(8))
                status, body, mime = self.get_image(uploaded["id"], "original", 0, max_edge=128)
                self.assertEqual(status, 200)
                self.assertEqual(mime, "image/jpeg")
                self.assertEqual(max(Image.open(io.BytesIO(body)).size), 128)
            finally:
                release.set()
            worker.join(10)
        self.assertTrue(release.is_set())
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.process_status, 200)
        photo = server.SESSIONS[uploaded["id"]]
        np.testing.assert_array_equal(photo.mask, control.mask)
        np.testing.assert_array_equal(photo.heal(), control.result)
        np.testing.assert_array_equal(photo.heal()[photo.mask == 0], cv2_speck[photo.mask == 0])

    def test_scaled_preview_blocks_export_until_full_auto_matches(self):
        """Wide scans get a scaled first compare. TIFF still matches full Auto alone."""
        original = np.full((96, 3072, 3), 130, np.uint8)
        cv2.circle(original, (400, 48), 3, (250, 250, 250), -1)
        raw = png(original)
        uploaded = self.upload(raw, "wide.png")
        photo = server.SESSIONS[uploaded["id"]]
        self.assertLess(photo.quick_scale, 1)
        revision = photo.revision
        mask = photo.mask.copy()
        kernels = []
        real_median = cv2.medianBlur

        def spy_median(src, ksize):
            kernels.append(int(ksize))
            return real_median(src, ksize)

        with patch("cv2.medianBlur", side_effect=spy_median):
            status, body, _ = self.post("/api/process", {"id": uploaded["id"], "level": "auto", "stage": "preview"})
        self.assertEqual(status, 200, body)
        info = json.loads(body)
        self.assertEqual(info["preview_stage"], "quick")
        self.assertFalse(info["export_ready"])
        self.assertEqual(info["revision"], revision)
        np.testing.assert_array_equal(photo.mask, mask)
        self.assertTrue(photo.quick_compare_pending_full)
        quick = info["quick"]
        self.assertEqual(quick["light_median"], scale_kernel(31, photo.quick_scale))
        self.assertLess(quick["light_median"], 31)
        self.assertEqual(quick["split_median"], scale_kernel(31, photo.quick_scale))
        self.assertEqual(quick["scaled_size"], scaled_dust_size(30, photo.quick_scale))
        self.assertLess(quick["scaled_size"], 30)
        self.assertEqual(int(quick["scaled_length"]), scale_length(1000, photo.quick_scale))
        self.assertLess(int(quick["scaled_length"]), 1000)
        self.assertIn(quick["light_median"], kernels)
        self.assertNotIn(31, kernels)
        status, denied, _ = self.post("/api/export", {"id": uploaded["id"], "revision": info["revision"], "format": "tiff"})
        self.assertEqual(status, 400)
        self.assertIn("完整结果尚未完成", json.loads(denied)["error"])
        preview_status, preview, mime = self.get_image(uploaded["id"], "result", info["revision"], max_edge=2048)
        self.assertEqual(preview_status, 200)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(preview.startswith(b"\xff\xd8"))
        self.assertLessEqual(max(Image.open(io.BytesIO(preview)).size), 2048)
        self.assertLess(max(Image.open(io.BytesIO(preview)).size), original.shape[1])
        scales = []
        real_detect = engine.detect_dust

        def spy_detect(*args, **kwargs):
            scales.append(kwargs.get("spatial_scale", 1))
            return real_detect(*args, **kwargs)

        with patch("engine.detect_dust", side_effect=spy_detect):
            status, body, _ = self.post("/api/process", {"id": uploaded["id"], "level": "auto"})
        self.assertEqual(status, 200, body)
        full = json.loads(body)
        self.assertEqual(scales, [1])
        self.assertEqual(full["preview_stage"], "full")
        self.assertTrue(full["export_ready"])
        self.assertFalse(photo.quick_compare_pending_full)
        self.assertGreater(full["revision"], revision)
        self.assertEqual(self.post("/api/export", {"id": uploaded["id"], "revision": revision, "format": "tiff"})[0], 400)
        status, exported, headers = self.post("/api/export", {"id": uploaded["id"], "revision": full["revision"], "format": "tiff"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/tiff")
        control = Photo(raw, "wide.png")
        control.process("auto")
        control_tiff = control.export("tiff")[0]
        self.assertEqual(hashlib.sha256(exported).hexdigest(), hashlib.sha256(control_tiff).hexdigest())
        np.testing.assert_array_equal(photo.mask, control.mask)
        np.testing.assert_array_equal(photo.heal(), control.result)
        decoded = tifffile.imread(io.BytesIO(exported))
        np.testing.assert_array_equal(decoded[photo.mask == 0], original[photo.mask == 0])


class ScaledPreviewThresholds(unittest.TestCase):
    def test_scale_one_keeps_full_resolution_literals(self):
        self.assertEqual(scale_kernel(31, 1), 31)
        self.assertEqual(scale_kernel(21, 1), 21)
        self.assertEqual(scale_length(1000, 1), 1000)
        self.assertEqual(scale_length(80, 1), 80)
        self.assertEqual(scaled_dust_size(30, 1), 30)
        image = np.full((180, 240, 3), 110, np.uint8)
        cv2.circle(image, (80, 70), 3, (250, 250, 250), -1)
        np.testing.assert_array_equal(
            detect_dust(image, 70, 30, True, True, "light"),
            detect_dust(image, 70, 30, True, True, "light", spatial_scale=1))

    def test_shorter_straight_band_is_held_only_when_length_is_scaled(self):
        comp = np.zeros((40, 160), np.uint8)
        cv2.line(comp, (12, 20), (72, 20), 255, 1)
        self.assertFalse(_straight_structure_band(comp, 1))
        self.assertTrue(_straight_structure_band(comp, 0.5))


if __name__ == "__main__":
    unittest.main()
