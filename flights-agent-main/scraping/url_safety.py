"""SSRF-safe URL validation for booking verification."""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

# Approved booking / metasearch hosts (lowercase, no scheme)
APPROVED_BOOKING_HOSTS = frozenset({
    "www.goindigo.in",
    "goindigo.in",
    "www.airindia.com",
    "airindia.com",
    "www.spicejet.com",
    "spicejet.com",
    "www.akasaair.com",
    "akasaair.com",
    "www.emirates.com",
    "emirates.com",
    "www.qatarairways.com",
    "qatarairways.com",
    "www.britishairways.com",
    "britishairways.com",
    "www.google.com",
    "google.com",
})

MAX_RESPONSE_BYTES = 512_000


class URLSafetyError(ValueError):
    """Raised when a URL fails safety checks."""


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_https_booking_url(url: str) -> str:
    """Validate scheme, host allowlist, and resolved IPs (SSRF mitigation)."""
    if not url or not isinstance(url, str):
        raise URLSafetyError("URL is required.")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise URLSafetyError("Only HTTPS booking URLs are permitted.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise URLSafetyError("URL has no hostname.")
    if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
        raise URLSafetyError("Local or private destinations are not allowed.")

    if host not in APPROVED_BOOKING_HOSTS:
        raise URLSafetyError(f"Host not in approved provider list: {host}")

    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if _is_blocked_ip(ip):
                raise URLSafetyError(f"Resolved IP {ip_str} is not allowed.")
    except socket.gaierror as e:
        raise URLSafetyError(f"Could not resolve host {host!r}.") from e

    return url.strip()


def safe_redirect_target(original: str, final: str) -> tuple[str, str]:
    """Validate original and post-redirect URL; returns (original, validated_final)."""
    validate_https_booking_url(original)
    if final and final != original:
        validate_https_booking_url(final)
    return original, final or original
