import threading
from datetime import datetime, timedelta
from typing import Dict, Optional


class CaptionEntry:
    def __init__(self, caption_data: dict):
        self.caption_data = caption_data
        self.created_at = datetime.utcnow()
        self.modified_at = self.created_at
        self.last_upsert_to_db_at: Optional[datetime] = None
        self.only_save_final_captions = True

    def update(self, caption_data: dict):
        self.caption_data = caption_data
        self.modified_at = datetime.utcnow()

    def should_upsert_to_db(self, should_flush=False) -> bool:
        if self.only_save_final_captions:
            if not self.caption_data.get("isFinal") and not should_flush:
                return False
            if not self.last_upsert_to_db_at:
                return True
            if self.modified_at > self.last_upsert_to_db_at:
                return True
            return False

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
        self.lock = threading.Lock()

    def upsert_caption(self, caption_data: dict):
        """
        Update or insert a caption into the in-memory store
        """
        caption_id = str(caption_data["captionId"])
        device_id = caption_data["deviceId"]
        key = f"{device_id}:{caption_id}"

        with self.lock:
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
        with self.lock:
            ready = []
            for key, entry in list(self.captions.items()):
                if entry.should_upsert_to_db(should_flush=should_flush):
                    ready.append(
                        (
                            key,
                            entry,
                            {
                                "deviceId": entry.caption_data["deviceId"],
                                "captionId": entry.caption_data["captionId"],
                                "text": entry.caption_data.get("text", ""),
                                "created_at": entry.created_at,
                                "modified_at": entry.modified_at,
                            },
                        )
                    )

        for key, entry, snap in ready:
            participant = self.get_participant_callback(snap["deviceId"])
            if not participant:
                continue

            self.save_utterance_callback(
                {
                    **participant,
                    "timestamp_ms": int(snap["created_at"].timestamp() * 1000),
                    "duration_ms": int((snap["modified_at"] - snap["created_at"]).total_seconds() * 1000),
                    "text": snap["text"],
                    "source_uuid_suffix": f"{snap['deviceId']}-{snap['captionId']}",
                    "sample_rate": None,
                }
            )

            with self.lock:
                # Skip stale work if the caption was removed or updated while callbacks ran
                if key not in self.captions or self.captions[key] is not entry:
                    continue
                if entry.modified_at != snap["modified_at"]:
                    continue
                entry.mark_upserted_to_db()
                if (datetime.utcnow() - entry.modified_at) > timedelta(seconds=60):
                    del self.captions[key]
