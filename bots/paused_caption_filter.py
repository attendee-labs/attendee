class PausedCaptionFilter:
    """Drops caption updates that would leak speech from a recording pause.

    Platform captions (Meet/Teams/Jitsi) arrive as *growing* segment texts keyed
    by ``deviceId:captionId``. Ignoring updates only while paused is not enough:
    the first update after resume carries the whole segment, including the words
    spoken during the pause. Any segment that sends an update during a pause is
    therefore frozen for good; segments that start after resume pass normally.
    """

    def __init__(self):
        self._frozen_keys = set()

    @staticmethod
    def key(caption):
        return f"{caption.get('deviceId')}:{caption.get('captionId')}"

    def should_drop(self, caption, recording_paused):
        key = self.key(caption)
        if recording_paused:
            self._frozen_keys.add(key)
            return True
        return key in self._frozen_keys
