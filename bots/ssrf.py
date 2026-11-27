"""Helpers to block server-side fetches to non-public network destinations (SSRF)."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Zoom RTMS signaling/media hosts observed in production. Override with
# ZOOM_RTMS_HOST_SUFFIX_ALLOWLIST (comma-separated). Set to empty string to
# skip the suffix check and rely only on public-IP resolution.
_DEFAULT_ZOOM_RTMS_HOST_SUFFIXES = (
    ".zoom.us",
    ".zoom.com",
    ".zoomgov.com",
    "zoom.us",
    "zoom.com",
    "zoomgov.com",
)


def _zoom_rtms_host_suffixes() -> tuple[str, ...] | None:
    raw = os.getenv("ZOOM_RTMS_HOST_SUFFIX_ALLOWLIST")
    if raw is None:
        return _DEFAULT_ZOOM_RTMS_HOST_SUFFIXES
    raw = raw.strip()
    if not raw:
        return None
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def hostname_resolves_public(hostname: str, port: int | None = None) -> bool:
    """True iff every A/AAAA for hostname is a global (public) unicast address."""
    if not hostname:
        return False

    # Literal IP in the URL — no DNS involved.
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_global
    except ValueError:
        pass

    try:
        addresses = socket.getaddrinfo(
            hostname,
            port or 443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return False

    if not addresses:
        return False

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return True


def url_is_public(url: str) -> bool:
    """
    Resolve the URL hostname and require every address to be globally routable.

    Rejects missing host, DNS failures, loopback, link-local, private RFC1918/ULA,
    and cloud metadata ranges covered by ipaddress.is_global == False.
    """
    parsed = urlsplit(url)
    if not parsed.hostname:
        return False
    return hostname_resolves_public(parsed.hostname, parsed.port)


def _host_matches_suffixes(hostname: str, suffixes: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    for suffix in suffixes:
        s = suffix.lower().rstrip(".")
        if host == s or host.endswith(s if s.startswith(".") else f".{s}"):
            return True
    return False


def assert_safe_rtms_websocket_url(url: str) -> str:
    """
    Validate a Zoom RTMS signaling/media WebSocket URL before the worker connects.

    - scheme must be wss://
    - hostname must match ZOOM_RTMS_HOST_SUFFIX_ALLOWLIST (unless disabled)
    - DNS must resolve only to public IPs
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("RTMS URL is required")
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "wss":
        raise ValueError("RTMS URL must use the wss:// scheme")
    if not parsed.hostname:
        raise ValueError("RTMS URL is missing a hostname")

    suffixes = _zoom_rtms_host_suffixes()
    if suffixes is not None and not _host_matches_suffixes(parsed.hostname, suffixes):
        raise ValueError(
            f"RTMS URL host {parsed.hostname!r} is not in the Zoom RTMS allowlist"
        )

    if not hostname_resolves_public(parsed.hostname, parsed.port or 443):
        raise ValueError(
            f"RTMS URL host {parsed.hostname!r} does not resolve to a public address"
        )

    return url


def assert_safe_https_media_url(url: str) -> str:
    """Validate a server-side HTTPS media fetch URL (e.g. output video MP4)."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Media URL is required")
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("Media URL must use the https:// scheme")
    if not parsed.hostname:
        raise ValueError("Media URL is missing a hostname")
    if not url_is_public(url):
        raise ValueError("Media URL must resolve to a public address")
    return url


def rtms_ssl_context():
    """
    SSL context for Zoom RTMS WebSockets.

    Defaults to certificate verification. Set ZOOM_RTMS_SSL_INSECURE=true only for
    broken lab environments (restores the previous CERT_NONE behaviour).
    """
    import ssl

    insecure = os.getenv("ZOOM_RTMS_SSL_INSECURE", "").lower() in ("1", "true", "yes")
    if insecure:
        logger.warning("ZOOM_RTMS_SSL_INSECURE enabled — TLS certificate verification disabled for RTMS")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()
