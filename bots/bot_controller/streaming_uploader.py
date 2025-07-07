import logging
import os
import threading
from io import BytesIO
from queue import Queue

from bots.storage import get_swift_client, get_container_name

logger = logging.getLogger(__name__)


class StreamingUploader:
    def __init__(self, bucket, key, chunk_size=5242880):  # 5MB chunks
        self.swift_client = get_swift_client()
        self.container = get_container_name()  # Use the configured container name
        self.key = key
        self.chunk_size = chunk_size
        self.buffer = BytesIO()
        self.upload_started = False
        
        # For Swift, we'll accumulate all data and upload at the end
        # Swift doesn't have the same multipart upload mechanism as S3

    def upload_part(self, data):
        """Add data to the buffer"""
        self.buffer.write(data)

    def complete_upload(self):
        """Upload the complete file to Swift"""
        try:
            # Get all buffered data
            self.buffer.seek(0)
            data = self.buffer.getvalue()
            
            if data:
                # Upload to Swift
                self.swift_client.put_object(
                    self.container,
                    self.key,
                    contents=data
                )
                logger.info(f"Successfully uploaded {len(data)} bytes to swift://{self.container}/{self.key}")
            else:
                logger.warning("No data to upload")
                
        except Exception as e:
            logger.error(f"Swift upload error: {e}")
            raise

    def start_upload(self):
        """Initialize the upload (no-op for Swift since we upload everything at once)"""
        self.upload_started = True
        logger.info(f"Initialized streaming upload for {self.key}")
