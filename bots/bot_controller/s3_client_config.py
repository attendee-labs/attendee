import os

from botocore.config import Config


def s3_addressing_style(endpoint_url: str | None) -> str:
    """Addressing style boto3 should use for `endpoint_url`.

    AWS serves virtual-hosted style (``<bucket>.s3.<region>.amazonaws.com``), and botocore
    folds the bucket into the hostname by default -- including when pointed at a custom
    endpoint. S3-compatible servers (rustfs, MinIO, Oracle Object Storage's ``compat``
    endpoint) generally only serve path style (``<endpoint>/<bucket>/<key>``), so a custom
    endpoint defaults to "path" here. ``AWS_S3_ADDRESSING_STYLE`` overrides either way, and
    matches the Django setting of the same name used by the django-storages backends.
    """
    configured = os.getenv("AWS_S3_ADDRESSING_STYLE")
    if configured:
        return configured
    return "path" if endpoint_url else "auto"


def s3_client_config(endpoint_url: str | None) -> Config:
    """Botocore config shared by the raw boto3 clients that upload bot media.

    signature_version is pinned to s3v4 because botocore otherwise negotiates SigV2 against a
    custom ``endpoint_url``, which S3-compatible servers reject with SignatureDoesNotMatch.
    The django-storages backends already get this from ``AWS_S3_SIGNATURE_VERSION`` in
    settings; these clients bypass Django settings entirely, so they set it themselves.
    """
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": s3_addressing_style(endpoint_url)},
    )
