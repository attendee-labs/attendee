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


class CaptionEntryGroup:
    def __init__(self, key: str, caption_data: dict):
        self.caption_entries: Dict[str, CaptionEntry] = {key: CaptionEntry(caption_data)}
        self.device_id: Optional[str] = caption_data["deviceId"]
        self.last_upsert_to_db_at: Optional[datetime] = None

    def merge_caption_entry(self, key: str, caption_data: dict):
        self.caption_entries[key] = CaptionEntry(caption_data)

    @property
    def modified_at(self):
        return max(entry.modified_at for entry in self.caption_entries.values())

    @property
    def created_at(self):
        return min(entry.created_at for entry in self.caption_entries.values())

    def mark_upserted_to_db(self):
        self.last_upsert_to_db_at = datetime.utcnow()
        for entry in self.caption_entries.values():
            entry.mark_upserted_to_db()

    def should_upsert_to_db(self, should_flush=False) -> bool:
        if not should_flush:
            # if it's been less than 1 second since we were modified, don't upsert
            if (datetime.utcnow() - self.modified_at) < timedelta(seconds=1):
                return False

        # If we can upsert all the children, do it
        for entry in self.caption_entries.values():
            if not entry.should_upsert_to_db(should_flush=should_flush):
                return False

        return True

    def get_text(self):
        return " ".join(entry.caption_data.get("text", "") for entry in sorted(self.caption_entries.values(), key=lambda x: x.created_at))


class GroupedClosedCaptionManager:
    def __init__(self, *, save_utterance_callback, get_participant_callback):
        self.caption_entry_groups: Dict[str, CaptionEntryGroup] = {}
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
            # Check if this caption is already in a group
            for group in self.caption_entry_groups.values():
                if group.caption_entries.get(key):
                    group.caption_entries[key].update(caption_data)
                    return

            # Check if the caption should be merged with any existing groups
            for group in self.caption_entry_groups.values():
                if group.device_id != device_id:
                    continue
                if group.modified_at + timedelta(seconds=1) > datetime.utcnow():
                    group.merge_caption_entry(key, caption_data)
                    return

            # If no opportunity to merge, create a new group
            self.caption_entry_groups[key] = CaptionEntryGroup(key, caption_data)

    def flush_captions(self):
        self.process_captions(should_flush=True)

    def process_captions(self, should_flush=False):
        """
        Process captions that are ready to be upserted to the database
        """
        with self.lock:
            ready = []
            for key, group in list(self.caption_entry_groups.items()):
                if group.should_upsert_to_db(should_flush=should_flush):
                    ready.append(
                        (
                            key,
                            group,
                            {
                                "device_id": group.device_id,
                                "text": group.get_text(),
                                "created_at": group.created_at,
                                "modified_at": group.modified_at,
                                "caption_id": group.caption_entries[key].caption_data["captionId"],
                            },
                        )
                    )

        for key, group, snap in ready:
            participant = self.get_participant_callback(snap["device_id"])
            if not participant:
                continue

            self.save_utterance_callback(
                {
                    **participant,
                    "timestamp_ms": int(snap["created_at"].timestamp() * 1000),
                    "duration_ms": int((snap["modified_at"] - snap["created_at"]).total_seconds() * 1000),
                    "text": snap["text"],
                    "source_uuid_suffix": f"{snap['device_id']}-{snap['caption_id']}",
                    "sample_rate": None,
                }
            )

            with self.lock:
                if key not in self.caption_entry_groups or self.caption_entry_groups[key] is not group:
                    continue
                if group.modified_at != snap["modified_at"]:
                    continue
                group.mark_upserted_to_db()
                if (datetime.utcnow() - group.modified_at) > timedelta(seconds=60):
                    del self.caption_entry_groups[key]
