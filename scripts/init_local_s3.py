"""Initialize and smoke-test the optional FakeCloud bucket (no real AWS access)."""

import os
from urllib.parse import urlparse
from uuid import uuid4

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError


def main():
    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if endpoint not in {"http://localhost.localstack.cloud:4566", "http://fakecloud:4566"}:
        raise SystemExit("Refusing non-local endpoint; use compose.fakecloud.yaml")
    bucket = os.environ.get("AWS_RECORDING_STORAGE_BUCKET_NAME", "attendee-local")
    s3 = boto3.client("s3", config=Config(s3={"addressing_style": "path"}))
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3.create_bucket(Bucket=bucket)

    key = f"checks/{uuid4().hex}.txt"
    data = b"Attendee local S3 smoke test"
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=data)
        actual = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if actual != data:
            raise RuntimeError("S3 GET returned unexpected contents")
        url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60)
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        if response.content != data:
            raise RuntimeError("Presigned GET returned unexpected contents")
        print(f"S3 PUT, GET and presigned GET passed at {urlparse(endpoint).netloc}; bucket={bucket}")
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            print("WARNING: HEAD returned 404 for an object readable by GET; see docs/local-arm64.md")
    finally:
        s3.delete_object(Bucket=bucket, Key=key)


if __name__ == "__main__":
    main()
