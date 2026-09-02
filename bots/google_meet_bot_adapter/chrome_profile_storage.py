import io
import logging
import os
import tarfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger(__name__)

# Directories inside the Chrome user-data-dir that are pure cache or Chrome
# component payloads (DRM modules, ML models, extension installers, …). They
# are recreated by Chrome on first launch and don't need to be persisted.
# Match by name at any depth: some of these (component_crx_cache, WidevineCdm,
# ...) live at the user-data-dir root, others (Cache, GPUCache, ...) live
# inside Default/. Keeping the tarball to the actual profile data
# (Default/Cookies, Default/Login Data, Default/Preferences, Local State, …)
# shrinks it from ~62 MB to ~1.5 MB.
_PROFILE_EXCLUDE_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "Service Worker",
    "ShaderCache",
    "GrShaderCache",
    "component_crx_cache",
    "WidevineCdm",
    "optimization_guide_model_store",
    "OnDeviceHeadSuggestModel",
    "hyphen-data",
    "ZxcvbnData",
    "CertificateRevocation",
}

# Lock files Chrome creates while running. They must not be included in the
# tarball, otherwise the next Chrome instance that loads the profile thinks
# another instance is already running.
_PROFILE_EXCLUDE_FILES = {
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "DevToolsActivePort",
    "chrome_debug.log",
    "BrowserMetrics-spare.pma",
}


def _is_cache_enabled():
    return getattr(settings, "CHROME_PROFILE_CACHE_ENABLED", True)


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL") or None,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY") or None,
    )


def _get_bucket_name():
    return os.getenv("AWS_CHROME_PROFILE_STORAGE_BUCKET_NAME") or os.getenv("AWS_RECORDING_STORAGE_BUCKET_NAME")


def _get_s3_key(login_domain):
    return f"chrome-profiles/{login_domain}.tar.gz"


def _should_exclude(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    # Match excluded dir names at any depth so top-level component dirs like
    # `component_crx_cache`/`WidevineCdm` are pruned along with `Default/Cache`.
    if any(part in _PROFILE_EXCLUDE_DIRS for part in parts):
        return True
    if path.name in _PROFILE_EXCLUDE_FILES:
        return True
    return False


def download_chrome_profile(login_domain, dest_dir):
    """Download and extract a cached Chrome profile from S3 into dest_dir.

    Returns True if a profile was downloaded and extracted, False otherwise
    (including when the profile doesn't exist in S3 or any error occurs).
    Never raises: profile reuse is an optimization, not a requirement.
    """
    if not _is_cache_enabled():
        return False
    bucket = _get_bucket_name()
    if not bucket:
        logger.info("No S3 bucket configured for Chrome profile storage, skipping download")
        return False

    s3_key = _get_s3_key(login_domain)
    try:
        s3_client = _get_s3_client()
        response = s3_client.get_object(Bucket=bucket, Key=s3_key)
        body = response["Body"].read()
        logger.info(f"Downloaded Chrome profile from s3://{bucket}/{s3_key} ({len(body)} bytes)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            logger.info(f"No cached Chrome profile found in S3 for domain {login_domain}")
        else:
            logger.warning(f"Error downloading Chrome profile from S3: {e}")
        return False
    except Exception as e:
        logger.warning(f"Error downloading Chrome profile from S3: {e}")
        return False

    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            tar.extractall(path=dest_dir)
        logger.info(f"Extracted Chrome profile to {dest_dir}")
        return True
    except Exception as e:
        logger.warning(f"Error extracting Chrome profile: {e}")
        return False


def upload_chrome_profile(login_domain, profile_dir):
    """Create a tarball of the Chrome profile at profile_dir and upload it to S3.

    Never raises: profile caching is an optimization, not a requirement.
    """
    if not _is_cache_enabled():
        return
    bucket = _get_bucket_name()
    if not bucket:
        logger.info("No S3 bucket configured for Chrome profile storage, skipping upload")
        return

    s3_key = _get_s3_key(login_domain)
    profile_path = Path(profile_dir)

    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for root, dirs, files in os.walk(profile_dir):
                root_path = Path(root)
                # Filter excluded dirs in-place so os.walk doesn't descend into them
                dirs[:] = [d for d in dirs if not _should_exclude(root_path / d, profile_path)]
                for file in files:
                    file_path = root_path / file
                    if _should_exclude(file_path, profile_path):
                        continue
                    arcname = file_path.relative_to(profile_path)
                    tar.add(file_path, arcname=str(arcname))

        body = buf.getvalue()
        s3_client = _get_s3_client()
        s3_client.put_object(Bucket=bucket, Key=s3_key, Body=body)
        logger.info(f"Uploaded Chrome profile to s3://{bucket}/{s3_key} ({len(body)} bytes)")
    except Exception as e:
        logger.warning(f"Error uploading Chrome profile to S3: {e}")