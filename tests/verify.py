"""Run with python3 verify.py. Checks the photo-quality and editing contracts."""
import io
import os
import unittest
from pathlib import Path

import cv2
import numpy as np
import tifffile
from PIL import Image, ImageCms

from engine import Photo, detect_dust, heal_mask

PRIVATE_SAMPLE = Path(os.environ.get("FILMDUST_PRIVATE_SAMPLE", str(Path(__file__).parent / "private/sample.jpeg")))
PRIVATE_EDGE = Path(os.environ.get("FILMDUST_PRIVATE_EDGE", str(Path(__file__).parent / "private/edge-case.npz")))


def photo_bytes(a):
    out = io.BytesIO()
    tifffile.imwrite(out, a, photometric="rgb" if a.ndim == 3 else "minisblack")
    return out.getvalue()


class QualityChecks(unittest.TestCase):
    def test_16bit_mask_only_and_export(self):
        rng = np.random.default_rng(42)
        a = rng.integers(8000, 50000, (80, 110, 3), dtype=np.uint16)
        p = Photo(photo_bytes(a), "scan.tif")
        p.edit("add", [[45, 40]], 6)
        result = p.heal()
        self.assertEqual(result.dtype, np.uint16)
        np.testing.assert_array_equal(result[p.mask == 0], a[p.mask == 0])
        self.assertTrue(np.any(result[p.mask > 0] != a[p.mask > 0]))
        data, _, _ = p.export("tiff")
        exported = tifffile.imread(io.BytesIO(data))
        np.testing.assert_array_equal(exported, result)
        with self.assertRaises(ValueError):
            p.export("png")

    def test_icc_png_round_trip(self):
        a = np.full((80, 100, 3), 123, np.uint8)
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        out = io.BytesIO()
        Image.fromarray(a).save(out, format="PNG", icc_profile=icc)
        p = Photo(out.getvalue(), "test.png")
        p.edit("add", [[50, 40]], 4)
        data, _, _ = p.export("png")
        im = Image.open(io.BytesIO(data))
        self.assertEqual(im.info["icc_profile"], icc)
        np.testing.assert_array_equal(np.asarray(im)[p.mask == 0], a[p.mask == 0])

    def test_cancel_undo_and_clear(self):
        p = Photo(photo_bytes(np.full((100, 100, 3), 128, np.uint8)), "scan.tif")
        p.edit("add", [[50, 50], [55, 55]], 6)
        saved = p.mask.copy()
        p.edit("reject", [[50, 50]], 10)
        self.assertFalse(np.any(p.mask))
        p.undo()
        np.testing.assert_array_equal(p.mask, saved)
        p.clear()
        self.assertFalse(np.any(p.mask))
        p.undo()
        np.testing.assert_array_equal(p.mask, saved)

    def test_dust_and_border_protection(self):
        a = np.full((300, 400, 3), [72, 117, 150], np.uint8)
        a[:20] = 0
        cv2.circle(a, (100, 100), 3, (245, 245, 245), -1)
        cv2.circle(a, (250, 140), 3, (5, 5, 5), -1)
        cv2.circle(a, (170, 210), 5, (245, 245, 245), -1)
        cv2.circle(a, (100, 8), 3, (245, 245, 245), -1)
        mask = detect_dust(a, protect=True)
        self.assertGreater(mask[100, 100], 0)
        self.assertGreater(mask[140, 250], 0)
        self.assertGreater(mask[210, 170], 0)
        self.assertEqual(mask[8, 100], 0)
        # A soft boundary is included, but no additional image feature is selected.
        self.assertLess(np.count_nonzero(mask), 700)
        self.assertEqual(cv2.connectedComponents(mask, 8)[0] - 1, 3)

    @unittest.skipUnless(PRIVATE_SAMPLE.is_file(), "Private scan fixture kept local")
    def test_real_scan_missed_defects_and_roof(self):
        p = Photo(PRIVATE_SAMPLE.read_bytes(), "scan.jpeg")
        mask = detect_dust(p.preview, polarity="light")
        # Manually located points in the user's real scan: long strand, small
        # curved strand, U-shaped strand, soft curled strand, faint dot and flake.
        anchors = [(1420, 992), (1486, 1024), (1531, 1080), (555, 869),
                   (2015, 981), (1452, 481), (1540, 455), (1520, 489),
                   (870, 755), (1576, 480)]
        for x, y in anchors:
            with self.subTest(point=(x, y)):
                self.assertTrue(np.any(mask[y - 5:y + 6, x - 5:x + 6]))
        # The densely repeated tile reflections shown in the second screenshot
        # should stay protected rather than being selected as hundreds of dots.
        self.assertLess(np.count_nonzero(mask[2440:2680, 780:1420]), 150)

    def test_alpha_and_gray_16bit(self):
        gray = np.full((100, 100), 32000, np.uint16)
        gray[50, 50] = 65000
        p = Photo(photo_bytes(gray), "gray.tif")
        p.edit("add", [[50, 50]], 4)
        healed = p.heal()
        self.assertEqual(healed[50, 50], 32000)
        rgba = np.full((100, 100, 4), 128, np.uint8)
        p = Photo(photo_bytes(rgba), "rgba.tif")
        p.edit("add", [[50, 50]], 4)
        np.testing.assert_array_equal(p.heal()[:, :, 3], rgba[:, :, 3])

    def test_tile_boundary(self):
        a = np.full((900, 900, 3), 12000, np.uint16)
        a[760:777, 760:777] = 60000
        mask = np.zeros(a.shape[:2], np.uint8)
        mask[759:778, 759:778] = 255
        result = heal_mask(a, mask)
        self.assertLess(np.max(result[760:777, 760:777]), 12100)
        np.testing.assert_array_equal(result[mask == 0], a[mask == 0])

    def test_orientation(self):
        a = np.full((50, 80, 3), 50, np.uint16)
        a[0, 0] = 60000
        buf = io.BytesIO()
        tifffile.imwrite(buf, a, photometric="rgb", extratags=[(274, "H", 1, 6, False)])
        p = Photo(buf.getvalue(), "oriented.tif")
        self.assertEqual((p.height, p.width), (80, 50))
        np.testing.assert_array_equal(p.original[0, -1], [60000, 60000, 60000])

    def test_exif_dpi_and_large_selection(self):
        im = Image.new("RGB", (80, 50), (120, 120, 120))
        exif = Image.Exif()
        exif[274] = 6
        exif[282] = 72.0
        exif[283] = 72.0
        buf = io.BytesIO()
        im.save(buf, format="JPEG", exif=exif, dpi=(72, 72))
        p = Photo(buf.getvalue(), "scan.jpg")
        self.assertEqual((p.width, p.height), (50, 80))
        data, _, _ = p.export("tiff")
        with tifffile.TiffFile(io.BytesIO(data)) as tf:
            self.assertEqual(tf.pages[0].tags["XResolution"].value, (72, 1))
        p.mask[:] = 255
        p.changed()
        with self.assertRaises(ValueError):
            p.heal()

    def test_straight_edge_remains_straight_16bit(self):
        a = np.full((120, 180, 3), [48000, 50000, 52000], np.uint16)
        a[60:] = [18000, 23000, 26000]
        truth = a.copy()
        mask = np.zeros(a.shape[:2], np.uint8)
        cv2.ellipse(mask, (90, 60), (9, 17), 15, 0, 360, 255, -1)
        a[mask > 0] = 65535
        report = {}
        out = heal_mask(a, mask, report=report)
        self.assertEqual(report["edge"], 1)
        self.assertEqual(report["pending"], [])
        np.testing.assert_array_equal(out, truth)

    @unittest.skipUnless(PRIVATE_EDGE.is_file(), "Private screenshot fixture kept local")
    def test_screenshot_edge_does_not_form_peak(self):
        fixture = np.load(PRIVATE_EDGE)
        a, mask = fixture["original"], fixture["mask"]
        report = {}
        out = heal_mask(a, mask, report=report)
        self.assertEqual(report["edge"], 1)
        lum = cv2.GaussianBlur(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32), (0, 0), 1)
        # Locate the actual boundary by its gradient, independently of local
        # brightness or grain; no artificial peak should be introduced.
        edge_rows = np.argmin(np.diff(lum, axis=0)[44:69, 29:54], axis=0)
        self.assertLessEqual(int(edge_rows.max() - edge_rows.min()), 3)
        np.testing.assert_array_equal(out[mask == 0], a[mask == 0])

    def test_clone_16bit_alpha_undo_and_erase(self):
        rng = np.random.default_rng(23)
        a = rng.integers(10000, 50000, (100, 100, 4), dtype=np.uint16)
        p = Photo(photo_bytes(a), "clone.tif")
        p.clone([20, 50], [[70, 50]], 10)
        mask = p.mask.copy()
        result = p.heal()
        np.testing.assert_array_equal(result[50, 70, :3], a[50, 20, :3])
        np.testing.assert_array_equal(result[:, :, 3], a[:, :, 3])
        np.testing.assert_array_equal(result[mask == 0], a[mask == 0])
        p.edit("erase", [[70, 50]], 2)
        np.testing.assert_array_equal(p.heal()[50, 70], a[50, 70])
        p.undo()
        np.testing.assert_array_equal(p.heal()[50, 70, :3], a[50, 20, :3])
        p.undo()
        self.assertFalse(np.any(p.mask))
        self.assertEqual(p.clone_strokes, [])

    def test_ambiguous_corner_stays_for_review(self):
        a = np.full((90, 90, 3), 170, np.uint8)
        a[45:, 45:] = 50
        mask = np.zeros((90, 90), np.uint8)
        cv2.circle(mask, (45, 45), 5, 255, -1)
        a[mask > 0] = 255
        report = {}
        out = heal_mask(a, mask, report=report)
        self.assertTrue(report["pending"])
        np.testing.assert_array_equal(out[mask > 0], a[mask > 0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
