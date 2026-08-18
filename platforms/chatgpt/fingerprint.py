"""统一的 ChatGPT Firefox 浏览器 profile。

The protocol registration path has three independently observable layers:
curl_cffi's TLS impersonation, HTTP request headers, and the browser/Sentinel
fingerprint.  Keeping their values here prevents a Firefox TLS handshake from
being paired with a Chrome UA or with a different browser environment.
"""
from __future__ import annotations

import re
from typing import Optional


# Firefox desktop version shared by curl_cffi and Camoufox.
FIREFOX_MAJOR = 144
FIREFOX_VERSION = f"{FIREFOX_MAJOR}.0"

# Keep both supported desktop OS spellings available.  The default profile is
# Windows so it remains stable across Windows and Linux hosts (Camoufox spoofs
# the selected OS independently of the host kernel).
FIREFOX_WINDOWS_PLATFORM = "Windows NT 10.0; Win64; x64"
FIREFOX_LINUX_PLATFORM = "X11; Linux x86_64"
FIREFOX_PLATFORM = FIREFOX_WINDOWS_PLATFORM
FIREFOX_OS = "windows"
FIREFOX_NAVIGATOR_PLATFORM = "Win32"
FIREFOX_OSCPU = FIREFOX_WINDOWS_PLATFORM
FIREFOX_LINUX_NAVIGATOR_PLATFORM = "Linux x86_64"
FIREFOX_LINUX_OSCPU = "Linux x86_64"

FIREFOX_WINDOWS_UA = (
    f"Mozilla/5.0 ({FIREFOX_WINDOWS_PLATFORM}; rv:{FIREFOX_VERSION}) "
    f"Gecko/20100101 Firefox/{FIREFOX_VERSION}"
)
FIREFOX_LINUX_UA = (
    f"Mozilla/5.0 ({FIREFOX_LINUX_PLATFORM}; rv:{FIREFOX_VERSION}) "
    f"Gecko/20100101 Firefox/{FIREFOX_VERSION}"
)

# The default UA is the one used by all protocol requests and Sentinel data.
FIREFOX_UA = FIREFOX_WINDOWS_UA

# Firefox's normal q-value for the fallback English locale.
FIREFOX_ACCEPT_LANGUAGE = "en-US,en;q=0.5"
FIREFOX_LANGUAGE = "en-US"
FIREFOX_LOCALES = ("en-US", "en")

# curl_cffi TLS profile and the corresponding browser version.
FIREFOX_IMPERSONATE = "firefox144"

# Sentinel/browser environment values.
FIREFOX_SCREEN_WIDTH = 1920
FIREFOX_SCREEN_HEIGHT = 1080
FIREFOX_SCREEN = f"{FIREFOX_SCREEN_WIDTH}x{FIREFOX_SCREEN_HEIGHT}"
SENTINEL_LANGUAGE = FIREFOX_LANGUAGE
SENTINEL_ACCEPT_LANGUAGE = FIREFOX_ACCEPT_LANGUAGE
SENTINEL_PLATFORM = FIREFOX_NAVIGATOR_PLATFORM
SENTINEL_SCREEN = FIREFOX_SCREEN
SENTINEL_SCROLL = "webkitTemporaryStorage\u2212undefined"
SENTINEL_LOCATION_TYPE = "location"
SENTINEL_OBJECT_PROTO = "Object"


_FIREFOX_UA_RE = re.compile(
    rf"^Mozilla/5\.0 \((?:{re.escape(FIREFOX_WINDOWS_PLATFORM)}|"
    rf"{re.escape(FIREFOX_LINUX_PLATFORM)}); rv:{re.escape(FIREFOX_VERSION)}\) "
    rf"Gecko/20100101 "
    rf"Firefox/{re.escape(FIREFOX_VERSION)}$"
)


def firefox_version(user_agent: Optional[str]) -> Optional[int]:
    """Return the Firefox major version from a UA, if it is well formed."""
    match = re.search(r"(?:Firefox|rv:)(\d+)(?:\.\d+)?", str(user_agent or ""))
    return int(match.group(1)) if match else None


def matches_tls_layer(impersonate: Optional[str]) -> bool:
    """Whether the TLS impersonation is the configured Firefox version."""
    value = str(impersonate or "").strip().lower()
    return value == FIREFOX_IMPERSONATE


def matches_ua_layer(user_agent: Optional[str]) -> bool:
    """Whether a UA is the configured Firefox desktop profile."""
    value = str(user_agent or "")
    return bool(_FIREFOX_UA_RE.fullmatch(value))


def matches_sentinel_layer(
    user_agent: Optional[str], expected_user_agent: Optional[str] = None
) -> bool:
    """Whether Sentinel uses the same Firefox UA as the HTTP headers."""
    value = str(user_agent or "")
    if expected_user_agent is not None and value != str(expected_user_agent):
        return False
    return matches_ua_layer(value)


def check_alignment(
    *,
    impersonate: Optional[str],
    user_agent: Optional[str],
    sentinel_user_agent: Optional[str] = None,
) -> dict:
    """Validate TLS, HTTP headers, and Sentinel/SO fingerprint alignment.

    Returns:
        A dict containing ``aligned`` and one boolean for each layer.
    """
    sentinel_ua = user_agent if sentinel_user_agent is None else sentinel_user_agent
    checks = {
        "tls": matches_tls_layer(impersonate),
        "headers": matches_ua_layer(user_agent),
        "sentinel": matches_sentinel_layer(sentinel_ua, user_agent),
    }
    return {"aligned": all(checks.values()), **checks}


__all__ = [
    "FIREFOX_ACCEPT_LANGUAGE",
    "FIREFOX_IMPERSONATE",
    "FIREFOX_LANGUAGE",
    "FIREFOX_LINUX_PLATFORM",
    "FIREFOX_LINUX_NAVIGATOR_PLATFORM",
    "FIREFOX_LINUX_OSCPU",
    "FIREFOX_LINUX_UA",
    "FIREFOX_LOCALES",
    "FIREFOX_MAJOR",
    "FIREFOX_NAVIGATOR_PLATFORM",
    "FIREFOX_OS",
    "FIREFOX_OSCPU",
    "FIREFOX_PLATFORM",
    "FIREFOX_SCREEN",
    "FIREFOX_SCREEN_HEIGHT",
    "FIREFOX_SCREEN_WIDTH",
    "FIREFOX_UA",
    "FIREFOX_VERSION",
    "FIREFOX_WINDOWS_PLATFORM",
    "FIREFOX_WINDOWS_UA",
    "SENTINEL_ACCEPT_LANGUAGE",
    "SENTINEL_LANGUAGE",
    "SENTINEL_LOCATION_TYPE",
    "SENTINEL_OBJECT_PROTO",
    "SENTINEL_PLATFORM",
    "SENTINEL_SCREEN",
    "SENTINEL_SCROLL",
    "check_alignment",
    "firefox_version",
    "matches_sentinel_layer",
    "matches_tls_layer",
    "matches_ua_layer",
]
