import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import close_old_connections


class AudioChunkUploader:
    """
    Simple in-process async uploader for audio chunks.

    - storage: any Django Storage instance (must implement .save(name, content)->stored_name)
    - on_success(stored_name): called after upload succeeds
    """

    def __init__(self, max_workers: int = 4, logger: Optional[logging.Logger] = None):
        self.storage = storages["audio_chunks"]
        self.log = logger or logging.getLogger(__name__)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="uploader")
        self._lock = threading.Lock()
        self._inflight = 0

    def upload(
        self,
        filename: str,
        data: bytes,
        on_success: Callable[[str], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        # track inflight (just for logging / observability)
        with self._lock:
            self._inflight += 1
            inflight = self._inflight

        self.log.info("AudioChunkUploader inflight=%s", inflight)

        fut = self._pool.submit(self._upload_one, filename, data)

        def _done(f):
            with self._lock:
                self._inflight -= 1
            try:
                stored_name = f.result()
            except Exception as e:
                self.log.exception("Upload failed for %s", filename)
                if on_error:
                    try:
                        on_error(e)
                    except Exception:
                        self.log.exception("on_error callback failed")
                return

            try:
                on_success(stored_name)
            except Exception:
                self.log.exception("on_success callback failed")

        fut.add_done_callback(_done)
        return fut

    def _upload_one(self, filename: str, data: bytes) -> str:
        # Django DB connections are thread-local; be safe if anything touches ORM indirectly
        close_old_connections()
        try:
            # storage.save may alter the name (avoid collisions), so use return value
            return self.storage.save(filename, ContentFile(data))
        finally:
            close_old_connections()

    def shutdown(self, wait: bool = True, cancel_futures: bool = False):
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)
