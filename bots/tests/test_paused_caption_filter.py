import unittest

from bots.paused_caption_filter import PausedCaptionFilter


class PausedCaptionFilterTest(unittest.TestCase):
    def test_segment_spanning_a_pause_stays_dropped_after_resume(self):
        f = PausedCaptionFilter()
        seg = {"captionId": "c1", "deviceId": "u1"}
        self.assertFalse(f.should_drop(seg, recording_paused=False))  # before pause: recorded
        self.assertTrue(f.should_drop(seg, recording_paused=True))  # during pause: dropped
        # After resume the same segment carries the paused words -> still dropped
        self.assertTrue(f.should_drop(seg, recording_paused=False))

    def test_new_segment_after_resume_passes(self):
        f = PausedCaptionFilter()
        f.should_drop({"captionId": "c1", "deviceId": "u1"}, recording_paused=True)
        self.assertFalse(f.should_drop({"captionId": "c2", "deviceId": "u1"}, recording_paused=False))

    def test_segments_are_keyed_per_device(self):
        f = PausedCaptionFilter()
        f.should_drop({"captionId": "c1", "deviceId": "u1"}, recording_paused=True)
        self.assertFalse(f.should_drop({"captionId": "c1", "deviceId": "u2"}, recording_paused=False))

    def test_untouched_segments_pass_when_not_paused(self):
        f = PausedCaptionFilter()
        self.assertFalse(f.should_drop({"captionId": "c1", "deviceId": "u1"}, recording_paused=False))


if __name__ == "__main__":
    unittest.main()
