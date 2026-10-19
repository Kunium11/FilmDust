"""Lossy progressive previews. Export stays lossless and detection stays put."""
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import numpy as np
import tifffile
from PIL import Image, ImageCms

import engine
import server
from engine import Photo, png_bytes


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


if __name__ == "__main__":
    unittest.main()
