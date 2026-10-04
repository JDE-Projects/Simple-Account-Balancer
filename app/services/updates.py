"""Update helpers and operations."""

import errno
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from app import config

def _update_error_reason(exc: BaseException) -> str:
    """Turn a check_update exception into a short, plain-language reason to
    show in the UI. Pure and network-free: takes the already-raised exception,
    never touches the network itself.

    Each branch is specific to a failure that can actually cause it, and
    names a next step where there is a sensible one. Subclasses are checked
    before their parents: SSLCertVerificationError and SSLEOFError/
    SSLZeroReturnError before the generic ssl.SSLError, and the specific
    ConnectionError subclasses and socket.gaierror before the generic OSError
    branch (socket.timeout is an alias of TimeoutError, and both are OSError
    subclasses)."""
    # HTTPError is a URLError subclass but carries its own .code, so classify
    # it before unwrapping anything.
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            return (
                "GitHub is rate-limiting update checks from this network. "
                "Try again later."
            )
        if exc.code == 404:
            return "No published release was found."
        if 500 <= exc.code < 600:
            return f"GitHub is having trouble on its end (HTTP {exc.code})."
        return f"GitHub returned an error (HTTP {exc.code})."

    if isinstance(exc, json.JSONDecodeError):
        return (
            "GitHub returned something unexpected. This often means a proxy "
            "or a guest wifi sign-in page answered instead."
        )

    # A plain URLError wraps the underlying cause (ssl.SSLError, socket.timeout,
    # a DNS/socket OSError, ...) in its .reason; unwrap it to classify the
    # actual cause, but remember it came from a URLError for the fallback below.
    is_url_error = isinstance(exc, urllib.error.URLError)
    cause = exc.reason if is_url_error and exc.reason is not None else exc

    if isinstance(cause, ssl.SSLCertVerificationError):
        return (
            "GitHub's certificate could not be verified. This usually means "
            "antivirus or a network filter is inspecting HTTPS traffic."
        )
    if isinstance(cause, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return "The secure connection was cut off during the handshake with GitHub."
    if isinstance(cause, ssl.SSLError):
        return "The secure connection to GitHub failed."
    if isinstance(cause, socket.gaierror):
        return (
            "The address for api.github.com could not be looked up. Check "
            "DNS or the internet connection."
        )
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return "GitHub didn't respond in time."
    if isinstance(cause, (ConnectionRefusedError, ConnectionResetError)):
        return (
            "The connection was refused or reset. A firewall or proxy may "
            "be blocking it."
        )
    if isinstance(cause, OSError) and getattr(cause, "errno", None) == errno.ENETUNREACH:
        return "No network connection."
    if is_url_error:
        return "Couldn't reach GitHub. Check the internet connection."

    text = f"{type(exc).__name__}: {exc}"
    if len(text) > 120:
        text = text[:117] + "..."
    return text

def _is_allowed_url(url) -> bool:
    """True only for a plain https address on the JDE-Projects website: no
    other host, scheme, port, login part, or whitespace/control characters."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    # Comparing the whole netloc rules out ports and user@host tricks.
    return parts.scheme == "https" and parts.netloc == config.ALLOWED_URL_HOST

def check_update(api):
    """Compare the latest published release to APP_VERSION. Quiet in the UI on
        failure (see _update_error_reason), but always logged when debug is on."""
    result = {"current": api._version, "version": None, "update": False, "offline": False}
    try:
        url = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        latest = (data.get("tag_name") or "").lstrip("v")
        result["version"] = latest
        if latest and api._is_newer(latest, api._version):
            result["update"] = True
        api.log(f"check_update: found v{latest}, current v{api._version}")
    except Exception as e:
        result["offline"] = True  # offline / private repo / rate-limited: stay quiet
        result["reason"] = _update_error_reason(e)
        api.log(f"check_update failed: {type(e).__name__}: {e}")
    return result


def _is_newer(latest: str, current: str) -> bool:
    def parts(v):
        out = []
        for p in v.split("."):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out
    return parts(latest) > parts(current)


def open_url(api, url: str):
    """Open a link in the system browser, never by navigating the app window.
        Only the JDE-Projects website is allowed (see _is_allowed_url)."""
    import webbrowser
    if not _is_allowed_url(url):
        api.log("open_url refused an address outside the allowed site")
        return {"ok": False, "error": "That link isn't allowed."}
    webbrowser.open(url)
    return {"ok": True}

