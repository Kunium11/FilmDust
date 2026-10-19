"""Opt-in large-JPEG timing report. The image check is skipped unless requested."""
import os
import unittest
from pathlib import Path

from timing_gate import enforce_enabled, judge_times, measure


class TimingReport(unittest.TestCase):
    def test_threshold_flags_are_a_report(self):
        flags = judge_times(12.5, 9.0, 10.0)
        self.assertFalse(flags["first_compare_within_target"])
        self.assertTrue(flags["final_preview_within_target"])
        self.assertEqual(flags["target_s"], 10.0)
        self.assertFalse(enforce_enabled({}))
        self.assertTrue(enforce_enabled({"FILMDUST_TIMING_ENFORCE": "1"}))

    @unittest.skipUnless(os.environ.get("FILMDUST_TIMING_GATE") == "1", "opt-in; set FILMDUST_TIMING_GATE=1 and FILMDUST_TIMING_SAMPLE")
    def test_sample_timing_is_reported(self):
        sample = os.environ.get("FILMDUST_TIMING_SAMPLE", "")
        self.assertTrue(sample and Path(sample).is_file(), "FILMDUST_TIMING_SAMPLE must be a local JPEG")
        report = measure(sample)
        print("\n" + "\n".join(f"{key}={report[key]}" for key in (
            "sample", "megapixels", "first_compare_s", "final_preview_s",
            "first_compare_within_target", "final_preview_within_target", "near_60mp")))
        self.assertGreater(report["megapixels"], 0)
        self.assertGreaterEqual(report["final_preview_s"], report["first_compare_s"])
        self.assertIn("machine", report)
        if enforce_enabled():
            self.assertTrue(report["near_60mp"], report["megapixels"])
            self.assertTrue(report["first_compare_within_target"], report["first_compare_s"])
            self.assertTrue(report["final_preview_within_target"], report["final_preview_s"])


if __name__ == "__main__":
    unittest.main()
