import logging
import os
import threading
from io import BytesIO
from queue import Queue

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)


class StreamingUploader:
    def __init__(self, bucket, key, chunk_size=5242880):  # 5MB chunks
        endpoint_url = os.getenv("AWS_ENDPOINT_URL")
        # Path-style addressing is required by some S3-compatible endpoints (MinIO,
        # LocalStack) but breaks others, so this is an explicit opt-in.
        config = Config(s3={"addressing_style": "path"}) if os.getenv("AWS_S3_USE_PATH_STYLE_ADDRESSING", "false") == "true" else None
        self.s3_client = boto3.client("s3", endpoint_url=endpoint_url, config=config)
        self.bucket = bucket
        self.key = key
        self.chunk_size = chunk_size
        self.buffer = BytesIO()
        self.upload_id = None
        self.parts = []
        self.part_number = 1

        # Add upload queue and worker thread
        self.upload_queue = Queue()
        self.upload_thread = threading.Thread(target=self._upload_worker, daemon=True)
        self.upload_thread.start()

    def _upload_worker(self):
        """Background thread to handle uploads"""
        while True:
            try:
                chunk, part_num = self.upload_queue.get()
                if chunk is None:  # Sentinel value to stop the thread
                    break

                response = self.s3_client.upload_part(
                    Bucket=self.bucket,
                    Key=self.key,
                    PartNumber=part_num,
                    UploadId=self.upload_id,
                    Body=chunk,
                )

                self.parts.append({"PartNumber": part_num, "ETag": response["ETag"]})
            except Exception as e:
                logging.error(f"Upload error: {e}")
            finally:
                self.upload_queue.task_done()

    def upload_part(self, data):
        self.buffer.write(data)

        # Upload complete chunks
        while self.buffer.tell() >= self.chunk_size:
            self.buffer.seek(0)
            chunk = self.buffer.read(self.chunk_size)

            # Queue the chunk for upload instead of uploading directly
            self.upload_queue.put((chunk, self.part_number))
            self.part_number += 1

            # Keep remaining data
            remaining = self.buffer.read()
            self.buffer = BytesIO()
            self.buffer.write(remaining)

    def complete_upload(self):
        # If we never started a multipart upload (len(self.parts) == 0), do a regular upload
        if len(self.parts) == 0:
            self.buffer.seek(0)
            data = self.buffer.getvalue()
            self.s3_client.put_object(Bucket=self.bucket, Key=self.key, Body=data)
            logger.info("len(self.parts) == 0, so did a regular upload")
            return

        # Upload final part if any data remains
        if self.buffer.tell() > 0:
            self.buffer.seek(0)
            final_chunk = self.buffer.getvalue()
            self.upload_queue.put((final_chunk, self.part_number))

        # Wait for all uploads to complete
        self.upload_queue.join()
        self.upload_queue.put((None, None))  # Stop the worker thread
        self.upload_thread.join()

        # Complete multipart upload
        self.s3_client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": sorted(self.parts, key=lambda x: x["PartNumber"])},
        )

    def start_upload(self):
        """Initialize the multipart upload and get the upload ID"""
        response = self.s3_client.create_multipart_upload(Bucket=self.bucket, Key=self.key)
        self.upload_id = response["UploadId"]
