"""Explicit manual repair, bounded work, exact undo and cropped previews."""
import io
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from PIL import Image
from engine import Photo, heal_mask


def photo(array):
    out=io.BytesIO();Image.fromarray(array).save(out,format='PNG')
    return Photo(out.getvalue(),'manual.png')


class LocalEditChecks(unittest.TestCase):
    def test_manual_repair_does_not_silently_keep_automatic_rejection(self):
        a=np.full((400,500,3),170,np.uint8);a[200:,250:]=50
        mask=np.zeros(a.shape[:2],np.uint8);cv2.circle(mask,(250,200),8,255,-1)
        a[mask>0]=255
        p=photo(a);p.install_detection(mask,np.zeros_like(mask),{})
        old=p.heal();self.assertTrue(p.heal_report['pending'])
        with patch('engine.detect_dust',side_effect=AssertionError('Manual edit ran detection')):
            with patch('engine.heal_mask',wraps=heal_mask) as called:
                with p.transaction():p.edit('add',[[250,200]],4)
                self.assertEqual(called.call_count,1)
                self.assertLess(called.call_args.args[0].shape[0],160)
                self.assertTrue(called.call_args.kwargs['manual'])
        self.assertTrue(np.any(p.heal()[200,250] != old[200,250]))
        self.assertEqual(p.heal_report['pending'],[])
        np.testing.assert_array_equal(p.heal()[mask==0],old[mask==0])
        with p.transaction():p.undo()
        np.testing.assert_array_equal(p.heal(),old)

    def test_touching_candidate_confirms_entire_component(self):
        a=np.full((300,400,3),120,np.uint8);cv2.circle(a,(150,130),12,(250,250,250),-1)
        pending=np.zeros(a.shape[:2],np.uint8);cv2.circle(pending,(150,130),14,255,-1)
        p=photo(a);p.install_detection(np.zeros_like(pending),pending,{})
        with p.transaction():p.edit('add',[[150,130]],4)
        self.assertFalse(p.pending_mask.any())
        self.assertLess(int(p.heal()[130,150].max()),125)
        self.assertEqual(p.preview_patch['bounds'],[136,116,29,29])

    def test_second_stroke_preserves_first_and_undo_is_exact(self):
        a=np.full((350,450,3),120,np.uint8)
        cv2.circle(a,(80,80),4,(250,250,250),-1);cv2.circle(a,(300,250),4,(250,250,250),-1)
        p=photo(a)
        with p.transaction():p.edit('add',[[80,80]],12)
        first=p.heal();first_revision=p.revision
        with p.transaction():p.edit('add',[[300,250]],12)
        self.assertEqual(p.preview_patch['from_revision'],first_revision)
        np.testing.assert_array_equal(p.heal()[:150,:150],first[:150,:150])
        self.assertLess(int(p.heal()[250,300].max()),125)
        with p.transaction():p.undo()
        np.testing.assert_array_equal(p.heal(),first)
        with p.transaction():p.undo()
        np.testing.assert_array_equal(p.heal(),a)

    def test_failure_restores_cached_manual_pixels_and_patch(self):
        a=np.full((160,160,3),120,np.uint8);a[80,80]=250
        p=photo(a)
        with p.transaction():p.edit('add',[[80,80]],6)
        before=p.heal();info=p.info()
        with self.assertRaises(ValueError):
            with p.transaction():p.edit('add',[[80,80]],100)
        np.testing.assert_array_equal(p.heal(),before);self.assertEqual(p.info(),info)


if __name__=='__main__':unittest.main()
