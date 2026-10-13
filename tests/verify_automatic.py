"""Real pixel protection, automatic processing and grouped undo contracts."""
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch
import cv2
import numpy as np
import tifffile
from engine import Photo, split_automatic_candidates

PRIVATE_SAMPLE = Path(os.environ.get("FILMDUST_PRIVATE_SAMPLE", str(Path(__file__).parent / "private/sample.jpeg")))
PRIVATE_EDGE = Path(os.environ.get("FILMDUST_PRIVATE_EDGE", str(Path(__file__).parent / "private/edge-case.npz")))


def make_photo(a):
 out=io.BytesIO();tifffile.imwrite(out,a,photometric='rgb' if a.ndim==3 else 'minisblack');return Photo(out.getvalue(),'scan.tif')


class AutomaticChecks(unittest.TestCase):
 def test_auto_16bit_keeps_held_and_unselected_pixels_and_can_export(self):
  a=np.full((240,320,3),23000,np.uint16);a[120:]=40000
  cv2.circle(a,(80,60),4,(65000,65000,65000),-1)
  cv2.circle(a,(170,119),4,(65000,65000,65000),-1)
  p=make_photo(a);p.process()
  self.assertEqual(p.result.dtype,np.uint16);self.assertTrue(np.any(p.mask[50:70,70:90]))
  np.testing.assert_array_equal(p.result[p.mask==0],a[p.mask==0])
  np.testing.assert_array_equal(p.result[p.pending_mask>0],a[p.pending_mask>0])
  data,_,_=p.export('tiff');np.testing.assert_array_equal(tifffile.imread(io.BytesIO(data)),p.result)
  self.assertEqual(p.info()['processed_count'],p.heal_report['local']+p.heal_report['edge']+p.heal_report['donor'])

 def test_edges_and_straight_structure_are_held_not_healed(self):
  rgb=np.full((240,320,3),90,np.uint8);rgb[120:]=160
  mask=np.zeros((240,320),np.uint8);cv2.circle(mask,(160,119),5,255,-1);cv2.line(mask,(50,50),(90,50),255,4)
  selected,pending,_=split_automatic_candidates(rgb,mask)
  self.assertFalse(np.any(selected));np.testing.assert_array_equal(mask,pending)

 def test_candidate_partition_is_complete_and_disjoint(self):
  rgb=np.full((200,240,3),120,np.uint8);mask=np.zeros(rgb.shape[:2],np.uint8)
  cv2.circle(mask,(80,70),4,255,-1);cv2.line(mask,(120,100),(170,100),255,3)
  selected,pending,_=split_automatic_candidates(rgb,mask)
  self.assertFalse(np.any((selected>0)&(pending>0)));np.testing.assert_array_equal(selected|pending,mask)

 def test_failure_preserves_prior_edits_and_undo(self):
  p=make_photo(np.full((120,140,3),120,np.uint8));p.edit('add',[[60,50]],8)
  before=p.mask.copy();history=len(p.history);revision=p.revision
  with patch('engine.heal_mask',side_effect=ValueError('fixture failure')):
   with self.assertRaises(ValueError):p.process()
  np.testing.assert_array_equal(p.mask,before);self.assertEqual(len(p.history),history);self.assertEqual(p.revision,revision)

 def test_keep_multiple_regions_is_one_undoable_action(self):
  p=make_photo(np.full((150,220,3),120,np.uint8));m=np.zeros_like(p.mask);pending=m.copy()
  cv2.circle(m,(40,70),5,255,-1);cv2.circle(pending,(120,70),5,255,-1)
  p.install_detection(m,pending,{});p.keep_regions([[40,70],[120,70]])
  self.assertFalse(np.any(p.mask|p.pending_mask));p.undo();np.testing.assert_array_equal(p.mask,m);np.testing.assert_array_equal(p.pending_mask,pending)

 @unittest.skipUnless(PRIVATE_SAMPLE.is_file(), "Private scan fixture kept local")
 def test_real_sample_structure_protection_and_known_defects(self):
  p=Photo(PRIVATE_SAMPLE.read_bytes(),'sample.jpeg');p.process()
  self.assertEqual(np.count_nonzero(p.mask[2440:2680,780:1420]),0)
  self.assertEqual(np.count_nonzero(p.mask[520:585,2100:2410]),0)
  for x,y in [(1420,992),(1486,1024),(1531,1080),(555,869),(2015,981),(1452,481),(1540,455),(1520,489),(870,755),(1576,480)]:
   with self.subTest(x=x,y=y):self.assertTrue(np.any((p.mask|p.pending_mask)[y-5:y+6,x-5:x+6]))
  self.assertTrue(p.info()['processed_count']>0);self.assertTrue(p.info()['reviews'])
  np.testing.assert_array_equal(p.result[p.pending_mask>0],p.original[p.pending_mask>0])

 def test_transparent_pixels_never_auto_selected(self):
  a=np.full((160,180,4),120,np.uint8);a[:,:,3]=255;a[50:90,50:90,3]=0
  a[60:70,60:70,:3]=255;p=make_photo(a);p.process();self.assertFalse(np.any((p.mask|p.pending_mask)[a[:,:,3]==0]))
  np.testing.assert_array_equal(p.result[:,:,3],a[:,:,3])

if __name__=='__main__':unittest.main(verbosity=2)
