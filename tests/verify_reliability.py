"""Precision, atomic edits and the staged document lifecycle regressions."""
import io
import json
import os
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer

import cv2
import numpy as np
import tifffile
from PIL import Image

from engine import Photo
import server


def png(array):
    out = io.BytesIO()
    Image.fromarray(array).save(out, format='PNG')
    return out.getvalue()


class PrecisionChecks(unittest.TestCase):
    def test_16bit_rgb_and_rgba_png_preserve_every_value(self):
        rng = np.random.default_rng(7)
        for channels in (3, 4):
            original = rng.integers(0, 65536, (64, 80, channels), dtype=np.uint16)
            bgr = original[:, :, [2, 1, 0, 3]] if channels == 4 else original[:, :, ::-1]
            raw = cv2.imencode('.png', bgr)[1].tobytes()
            photo = Photo(raw, 'scan.png')
            self.assertEqual(photo.depth, 16)
            np.testing.assert_array_equal(photo.original, original)
            np.testing.assert_array_equal(tifffile.imread(io.BytesIO(photo.export('tiff')[0])), original)

    def test_gray_alpha_tiff_is_one_page_and_round_trips(self):
        for depth in (8, 16):
            dtype = np.uint8 if depth == 8 else np.uint16
            original = np.zeros((64, 80, 2), dtype)
            original[:, :, 0] = np.arange(80, dtype=dtype) * (1 if depth == 8 else 700)
            original[:, :, 1] = np.iinfo(dtype).max // 2
            if depth == 8:
                raw = png(original)
            else:
                stream = io.BytesIO()
                tifffile.imwrite(stream, original, photometric='minisblack', planarconfig='contig', extrasamples=['UNASSALPHA'])
                raw = stream.getvalue()
            photo = Photo(raw, 'gray-alpha')
            exported = photo.export('tiff')[0]
            with tifffile.TiffFile(io.BytesIO(exported)) as tf:
                self.assertEqual(len(tf.pages), 1)
                self.assertEqual(tf.pages[0].extrasamples[0].name, 'UNASSALPHA')
                np.testing.assert_array_equal(tf.asarray(), original)
            np.testing.assert_array_equal(Photo(exported, 'roundtrip.tif').original, original)

    def test_associated_alpha_preview_export_and_repair(self):
        original = np.full((64, 80, 4), 64, np.uint8)
        original[:, :, 3] = 128
        original[30:34, 30:34, :3] = 128
        stream = io.BytesIO()
        tifffile.imwrite(stream, original, photometric='rgb', extrasamples=['ASSOCALPHA'])
        photo = Photo(stream.getvalue(), 'associated.tif')
        self.assertEqual(int(photo.preview[0, 0, 0]), 128)
        exported = photo.export('tiff')[0]
        with tifffile.TiffFile(io.BytesIO(exported)) as tf:
            self.assertEqual(tf.pages[0].extrasamples[0].name, 'ASSOCALPHA')
            np.testing.assert_array_equal(tf.asarray(), original)
        p = np.asarray(Image.open(io.BytesIO(photo.export('png')[0])))
        np.testing.assert_array_equal(p[0, 0], [128, 128, 128, 128])
        with photo.transaction():
            photo.edit('add', [[32, 32]], 10)
        result = photo.heal()
        np.testing.assert_array_equal(result[photo.mask == 0], original[photo.mask == 0])
        np.testing.assert_array_equal(result[:, :, 3], original[:, :, 3])
        self.assertLess(int(result[32, 32, 0]), 90)

    def test_failed_edit_restores_result_revision_and_history(self):
        photo = Photo(png(np.full((160, 160, 3), 120, np.uint8)), 'plain.png')
        photo.process('auto')
        photo.undo()
        photo.heal()
        before = photo.info()
        with self.assertRaisesRegex(ValueError, '15%'):
            with photo.transaction():
                photo.edit('add', [[80, 80]], 100)
        self.assertEqual(photo.info(), before)
        self.assertFalse(np.any(photo.mask))

    @unittest.skipUnless(os.environ.get('FILMDUST_PRIVATE_SAMPLE'), 'local scan not provided')
    def test_auto_clears_known_faint_dust_and_preserves_scene(self):
        photo = Photo(Path(os.environ['FILMDUST_PRIVATE_SAMPLE']).read_bytes(), 'scan.jpg')
        photo.process('auto')
        for x, y in [(1420,992),(1486,1024),(1531,1080),(555,869),(2015,981),(1452,481),(1540,455),(1520,489),(870,755),(1576,480)]:
            self.assertTrue(np.any(photo.heal()[y-5:y+6, x-5:x+6] != photo.original[y-5:y+6, x-5:x+6]), (x, y))
        # The old rectangle also included a real speck on a flat wall at 781,2672.
        # Keep the architectural texture interior protected, not that speck.
        self.assertFalse(np.any(photo.mask[2440:2640,800:1420]))
        np.testing.assert_array_equal(photo.result[photo.mask == 0], photo.original[photo.mask == 0])


class SessionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.service.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.service.server_port)

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
        request = urllib.request.Request(self.base + path, data=data, headers={'X-FilmDust-Token':server.TOKEN,'Origin':self.base})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def upload(self, retain=''):
        status, raw = self.post('/api/upload?name=plain.png&retain='+retain, raw=png(np.full((160,160,3), 120, np.uint8)))
        self.assertEqual(status, 200)
        return json.loads(raw)['id']

    def test_repeated_failed_switches_and_lost_activation_reply(self):
        active = self.upload()
        self.post('/api/activate', {'id':active})
        for _ in range(3):
            pending = self.upload(active)
            # Even if activation succeeded but its reply was lost, the client
            # can keep showing the earlier document and protects it next upload.
            self.post('/api/activate', {'id':pending})
        self.assertEqual(self.post('/api/info', {'id':active})[0], 200)
        self.assertLessEqual(len(server.SESSIONS), 3)
        self.post('/api/release', {'id':pending})
        self.assertEqual(self.post('/api/export', {'id':active,'revision':0,'format':'tiff'})[0], 200)

    def test_atomic_edit_and_stale_export_block(self):
        ident = self.upload()
        before = self.post('/api/info', {'id':ident})[1]
        status, _ = self.post('/api/edit', {'id':ident,'mode':'add','points':[[80,80]],'diameter':100,'revision':0})
        self.assertEqual(status, 400)
        self.assertEqual(self.post('/api/info', {'id':ident})[1], before)
        status, _ = self.post('/api/edit', {'id':ident,'mode':'add','points':[[80,80]],'diameter':10,'revision':0})
        self.assertEqual(status, 200)
        self.assertEqual(self.post('/api/export', {'id':ident,'revision':0})[0], 400)
        self.assertEqual(self.post('/api/export', {'id':ident,'revision':1})[0], 200)

    def test_local_preview_matches_export_and_rejects_stale_revision(self):
        ident = self.upload()
        status, raw = self.post('/api/edit', {'id':ident,'mode':'add','points':[[80,80]],'diameter':10,'revision':0})
        self.assertEqual(status, 200)
        info = json.loads(raw)
        x, y, w, h = info['preview_patch']['bounds']
        self.assertEqual(info['preview_patch']['from_revision'], 0)
        rect = ','.join(str(v) for v in (x,y,w,h))
        url = self.base + '/api/image?id=' + ident + '&kind=result&v=1&rect=' + rect
        with urllib.request.urlopen(url) as response:
            preview = np.asarray(Image.open(io.BytesIO(response.read())))
        exported = tifffile.imread(io.BytesIO(self.post('/api/export', {'id':ident,'revision':1})[1]))
        np.testing.assert_array_equal(preview, exported[y:y+h,x:x+w])
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(url.replace('&v=1', '&v=0'))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(url.replace('&rect='+rect, '&rect=-1,0,10,10'))


if __name__ == '__main__':
    unittest.main()
