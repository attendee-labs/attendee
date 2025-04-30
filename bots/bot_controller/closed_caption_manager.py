from datetime import datetime, timedelta
from typing import Dict, Optional


class CaptionEntry:
    def __init__(self, caption_data: dict):
        self.caption_data = caption_data
        self.created_at = datetime.utcnow()
        self.modified_at = self.created_at
        self.last_upsert_to_db_at: Optional[datetime] = None

    def update(self, caption_data: dict):
        self.caption_data = caption_data
        self.modified_at = datetime.utcnow()

    def should_upsert_to_db(self, should_flush=False) -> bool:
        # If never upserted to db, and it's been at least a second since creation
        if not self.last_upsert_to_db_at:
            return ((datetime.utcnow() - self.created_at) > timedelta(seconds=1)) or should_flush

        # If modified since last upsert to db and hasn't been updated recently
        return self.modified_at > self.last_upsert_to_db_at and (((datetime.utcnow() - self.modified_at) > timedelta(seconds=2)) or should_flush)

    def mark_upserted_to_db(self):
        self.last_upsert_to_db_at = datetime.utcnow()


class ClosedCaptionManager:
    def __init__(self, *, save_utterance_callback, get_participant_callback):
        self.captions: Dict[str, CaptionEntry] = {}
        self.save_utterance_callback = save_utterance_callback
        self.get_participant_callback = get_participant_callback

    def upsert_caption(self, caption_data: dict):
        """
        Update or insert a caption into the in-memory store
        """
        caption_id = str(caption_data["captionId"])
        device_id = caption_data["deviceId"]
        key = f"{device_id}:{caption_id}"

        if key in self.captions:
            self.captions[key].update(caption_data)
        else:
            self.captions[key] = CaptionEntry(caption_data)

    def flush_captions(self):
        self.process_captions(should_flush=True)

    def process_captions(self, should_flush=False):
        """
        Process captions that are ready to be upserted to the database
        """
        for key, entry in list(self.captions.items()):
            if entry.should_upsert_to_db(should_flush=should_flush):
                device_id = entry.caption_data["deviceId"]
                participant = self.get_participant_callback(device_id)

                if participant:
                    # Save as an utterance
                    self.save_utterance_callback(
                        {
                            **participant,
                            "timestamp_ms": int(entry.created_at.timestamp() * 1000),
                            "duration_ms": int((entry.modified_at - entry.created_at).total_seconds() * 1000),
                            "text": entry.caption_data.get("text", ""),
                            "source_uuid_suffix": f"{entry.caption_data['deviceId']}-{entry.caption_data['captionId']}",
                            "sample_rate": None,
                        }
                    )

                    # Mark as upserted and remove if it hasn't been modified recently
                    entry.mark_upserted_to_db()

                    # If this caption hasn't been modified in a while, remove it from memory
                    if (datetime.utcnow() - entry.modified_at) > timedelta(seconds=60):
                        del self.captions[key]
