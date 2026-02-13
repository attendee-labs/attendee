import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional

from django.core.files.base import ContentFile
from django.core.files.storage import storages


class AudioChunkUploader:
    """
    Simple in-process async uploader for audio chunks.

    - storage: any Django Storage instance (must implement .save(name, content)->stored_name)
    - Uploads are queued via upload() and processed via process_uploads() from the main thread
    """

    def __init__(
        self,
        on_success: Callable[[int, str], None],
        on_error: Optional[Callable[[int, Exception], None]] = None,
        max_workers: int = 4,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Args:
            on_success: Called with (audio_chunk_id, stored_name) for each successful upload
            on_error: Called with (audio_chunk_id, exception) for each failed upload
            max_workers: Maximum number of concurrent upload threads
            logger: Optional logger instance
        """
        self._on_success = on_success
        self._on_error = on_error
        self.storage = storages["audio_chunks"]
        self.log = logger or logging.getLogger(__name__)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="uploader")
        self._lock = threading.Lock()
        self._pending_uploads: Dict[int, Dict] = {}  # audio_chunk_id -> upload info

    def upload(
        self,
        audio_chunk_id: int,
        filename: str,
        data: bytes,
    ):
        """
        Queue an upload for an audio chunk.

        Args:
            audio_chunk_id: The ID of the AudioChunk record
            filename: The target filename in storage
            data: The audio data bytes to upload
        """
        fut = self._pool.submit(self._upload_one, filename, data)

        with self._lock:
            self._pending_uploads[audio_chunk_id] = {
                "future": fut,
                "filename": filename,
            }
            inflight = len(self._pending_uploads)

        self.log.info("AudioChunkUploader queued audio_chunk_id=%s, inflight=%s", audio_chunk_id, inflight)

    def process_uploads(self):
        """
        Process completed uploads. Call this from the main thread at regular intervals.

        Iterates over completed uploads, removes them from the in-memory store,
        and calls the appropriate callback.
        """
        completed = []

        with self._lock:
            for audio_chunk_id, upload_info in list(self._pending_uploads.items()):
                fut = upload_info["future"]
                if fut.done():
                    completed.append((audio_chunk_id, upload_info))
                    del self._pending_uploads[audio_chunk_id]

        for audio_chunk_id, upload_info in completed:
            fut = upload_info["future"]
            try:
                stored_name = fut.result()
                try:
                    self._on_success(audio_chunk_id, stored_name)
                except Exception:
                    self.log.exception("on_success callback failed for audio_chunk_id=%s", audio_chunk_id)
            except Exception as e:
                self.log.exception("Upload failed for audio_chunk_id=%s, filename=%s", audio_chunk_id, upload_info["filename"])
                if self._on_error:
                    try:
                        self._on_error(audio_chunk_id, e)
                    except Exception:
                        self.log.exception("on_error callback failed for audio_chunk_id=%s", audio_chunk_id)

    def _upload_one(self, filename: str, data: bytes) -> str:
        # storage.save may alter the name (avoid collisions), so use return value
        return self.storage.save(filename, ContentFile(data))

    def shutdown(self, wait: bool = True, cancel_futures: bool = False):
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    def wait_for_uploads(self, timeout: float = 5.0):
        """
        Wait for all pending uploads to complete, processing them as they finish.

        Args:
            timeout: Maximum time to wait in seconds (default 5.0)
        """
        import time

        start_time = time.time()
        while True:
            self.process_uploads()

            with self._lock:
                pending_count = len(self._pending_uploads)

            if pending_count == 0:
                self.log.info("wait_for_uploads: all uploads completed")
                return

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                self.log.warning("wait_for_uploads: timeout after %.1fs with %d uploads still pending", elapsed, pending_count)
                # Call on_error for all pending uploads
                for audio_chunk_id, upload_info in self._pending_uploads.items():
                    if self._on_error:
                        try:
                            self._on_error(audio_chunk_id, Exception("In wait_for_uploads, upload timed out"))
                        except Exception:
                            self.log.exception("on_error callback failed for audio_chunk_id=%s", audio_chunk_id)
                return

            time.sleep(0.1)
