"""Patch undo/redo restores pixels bit for bit and keeps only changed rectangles."""
import io
import unittest

import cv2
import numpy as np
import tifffile
from PIL import Image

from engine import UNDO_STEP_LIMIT, Photo, _trim_stack


def photo_bytes(array):
    out = io.BytesIO()
    if array.dtype == np.uint8:
        Image.fromarray(array).save(out, format="PNG")
    else:
        tifffile.imwrite(out, array, photometric="rgb" if array.ndim == 3 else "minisblack")
    return Photo(out.getvalue(), "scan")


class PatchUndo(unittest.TestCase):
    def test_manual_16bit_undo_and_redo_are_bit_exact(self):
        rng = np.random.default_rng(5)
        original = rng.integers(0, 65536, (90, 120, 3), dtype=np.uint16)
        original[40:48, 50:60] = 64000
        photo = photo_bytes(original)
        with photo.transaction():
            photo.edit("add", [[55, 44]], 8)
        repaired = photo.heal().copy()
        mask = photo.mask.copy()
        self.assertTrue(np.any(repaired != original))
        np.testing.assert_array_equal(repaired[mask == 0], original[mask == 0])
        entry = photo.history[-1]
        self.assertTrue(entry["sealed"])
        self.assertNotIn("result", entry)
        self.assertLess(photo.undo_step_bytes()[-1], original.nbytes // 4)
        before_export = photo.export("tiff")[0]
        revision = photo.revision
        bounds = list(photo.preview_patch["bounds"])
        with photo.transaction():
            photo.undo()
        self.assertEqual(photo.preview_patch["from_revision"], revision)
        self.assertEqual(photo.preview_patch["bounds"], bounds)
        np.testing.assert_array_equal(photo.heal(), original)
        self.assertFalse(np.any(photo.mask))
        with photo.transaction():
            photo.redo()
        np.testing.assert_array_equal(photo.heal(), repaired)
        np.testing.assert_array_equal(photo.mask, mask)
        self.assertEqual(photo.export("tiff")[0], before_export)
        with tifffile.TiffFile(io.BytesIO(before_export)) as tf:
            decoded = tf.asarray()
        self.assertEqual(decoded.dtype, np.uint16)
        np.testing.assert_array_equal(decoded, repaired)
        np.testing.assert_array_equal(decoded[mask == 0], original[mask == 0])

    def test_two_steps_round_trip_and_a_new_edit_drops_redo(self):
        original = np.full((160, 200, 3), 90, np.uint8)
        original[30:40, 30:40] = 250
        original[120:130, 150:160] = 250
        photo = photo_bytes(original)
        with photo.transaction():
            photo.edit("add", [[35, 35]], 8)
        first = photo.heal().copy()
        first_mask = photo.mask.copy()
        with photo.transaction():
            photo.edit("add", [[155, 125]], 8)
        second = photo.heal().copy()
        second_mask = photo.mask.copy()
        np.testing.assert_array_equal(second[20:50, 20:50], first[20:50, 20:50])
        with photo.transaction():
            photo.undo()
        np.testing.assert_array_equal(photo.heal(), first)
        np.testing.assert_array_equal(photo.mask, first_mask)
        with photo.transaction():
            photo.undo()
        np.testing.assert_array_equal(photo.heal(), original)
        self.assertFalse(np.any(photo.mask))
        with photo.transaction():
            photo.redo()
        np.testing.assert_array_equal(photo.heal(), first)
        with photo.transaction():
            photo.redo()
        np.testing.assert_array_equal(photo.heal(), second)
        np.testing.assert_array_equal(photo.mask, second_mask)
        with photo.transaction():
            photo.undo()
        self.assertTrue(photo.info()["redo"])
        with photo.transaction():
            photo.edit("add", [[80, 80]], 6)
        self.assertFalse(photo.info()["redo"])
        photo.redo()
        self.assertTrue(np.any(photo.mask[70:90, 70:90]))

    def test_auto_patches_cover_distant_specks_without_a_full_frame(self):
        original = np.full((360, 480, 3), 110, np.uint8)
        cv2.circle(original, (90, 250), 3, (250, 250, 250), -1)
        cv2.circle(original, (420, 40), 3, (250, 250, 250), -1)
        photo = photo_bytes(original)
        with photo.transaction():
            photo.process("auto")
        self.assertGreater(photo.mask[250, 90], 0)
        self.assertGreater(photo.mask[40, 420], 0)
        entry = photo.history[-1]
        self.assertTrue(entry["sealed"])
        self.assertNotIn("result", entry)
        self.assertGreaterEqual(len(entry["patches"]), 2)
        self.assertLess(photo.undo_step_bytes()[-1], original.nbytes // 10)
        repaired = photo.heal().copy()
        mask = photo.mask.copy()
        pending = photo.pending_mask.copy()
        np.testing.assert_array_equal(repaired[mask == 0], original[mask == 0])
        result_patch = next(patch for patch in entry["patches"] if "result" in patch)
        y, x, h, w = result_patch["y"], result_patch["x"], result_patch["h"], result_patch["w"]
        stored = result_patch["result"].copy()
        photo.result[y:y + h, x:x + w] = 0
        np.testing.assert_array_equal(result_patch["result"], stored)
        photo.result[:] = repaired
        with photo.transaction():
            photo.undo()
        np.testing.assert_array_equal(photo.heal(), original)
        self.assertFalse(np.any(photo.mask | photo.pending_mask))
        with photo.transaction():
            photo.redo()
        np.testing.assert_array_equal(photo.heal(), repaired)
        np.testing.assert_array_equal(photo.mask, mask)
        np.testing.assert_array_equal(photo.pending_mask, pending)

    def test_failed_edit_restores_redo_stack(self):
        photo = photo_bytes(np.full((100, 100, 3), 128, np.uint8))
        with photo.transaction():
            photo.edit("add", [[20, 20]], 6)
        with photo.transaction():
            photo.undo()
        self.assertTrue(photo.info()["redo"])
        self.assertFalse(photo.info()["undo"])
        before = photo.info()
        history = len(photo.history)
        with self.assertRaisesRegex(ValueError, "15%"):
            with photo.transaction():
                photo.edit("add", [[50, 50]], 80)
        self.assertEqual(photo.info(), before)
        self.assertEqual(len(photo.history), history)
        np.testing.assert_array_equal(photo.heal(), photo.original)

    def test_step_limit_and_byte_budget_drop_oldest_patches(self):
        photo = photo_bytes(np.full((96, 128, 3), 40, np.uint8))
        for index in range(UNDO_STEP_LIMIT + 1):
            with photo.transaction():
                photo.edit("add", [[8 + index * 6, 16]], 4)
        self.assertEqual(len(photo.history), UNDO_STEP_LIMIT)
        self.assertTrue(all(entry["sealed"] and "result" not in entry for entry in photo.history))
        stack = [{"sealed": True, "patches": [{"result": np.zeros((10, 10, 3), np.uint8)}], "clone_strokes": []} for _ in range(5)]
        _trim_stack(stack, budget=700, limit=12)
        self.assertEqual(len(stack), 2)
        huge = [{"sealed": True, "patches": [{"result": np.zeros((80, 80, 3), np.uint8)}], "clone_strokes": []}]
        _trim_stack(huge, budget=10, limit=12)
        self.assertEqual(len(huge), 1)

    def test_clone_redo_restores_16bit_alpha_pixels(self):
        rng = np.random.default_rng(9)
        original = rng.integers(1000, 50000, (80, 100, 4), dtype=np.uint16)
        photo = photo_bytes(original)
        with photo.transaction():
            photo.clone([15, 40], [[70, 40]], 8)
        repaired = photo.heal().copy()
        with photo.transaction():
            photo.undo()
        np.testing.assert_array_equal(photo.heal(), original)
        self.assertEqual(photo.clone_strokes, [])
        with photo.transaction():
            photo.redo()
        np.testing.assert_array_equal(photo.heal(), repaired)
        np.testing.assert_array_equal(photo.heal()[:, :, 3], original[:, :, 3])
        np.testing.assert_array_equal(photo.heal()[photo.mask == 0], original[photo.mask == 0])


if __name__ == "__main__":
    unittest.main()
