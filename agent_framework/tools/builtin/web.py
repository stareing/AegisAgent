"""Built-in web tools.

Provides web page fetching with content extraction and SSRF protection.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agent_framework.tools.decorator import tool

_MAX_CONTENT_CHARS = 80_000
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
_READ_CHUNK_SIZE = 65_536
_DEFAULT_TIMEOUT = 30
_USER_AGENT = "AegisAgent/0.1 (Python)"

_DECODABLE_CONTENT_TYPES = {"text/", "application/json"}


def _check_ssrf(url: str) -> None:
    """Block requests to private, loopback, and link-local addresses."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Cannot extract hostname from URL: {url}")

    try:
        addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname '{hostname}': {exc}") from exc

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        addr = ipaddress.ip_address(ip_str)
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            raise ValueError(
                f"URL '{url}' resolves to blocked address {ip_str}. "
                "Requests to private, loopback, link-local, reserved, "
                "and multicast addresses are not allowed."
            )


def _is_decodable_content_type(content_type: str) -> bool:
    """Return True if the content type should be decoded as text."""
    ct_lower = content_type.lower()
    return any(prefix in ct_lower for prefix in _DECODABLE_CONTENT_TYPES)


def _streaming_read(resp, max_bytes: int = _MAX_RESPONSE_BYTES) -> bytes:
    """Read response body in chunks, enforcing a maximum size."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            # Keep only up to the limit
            overshoot = total - max_bytes
            chunks.append(chunk[: len(chunk) - overshoot])
            break
        chunks.append(chunk)
    return b"".join(chunks)


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "svg", "head"}
        self._title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip = True
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags:
            self._skip = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title = data.strip()
        if not self._skip:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse multiple blank lines.
        return re.sub(r"\n{3,}", "\n\n", raw).strip()

    def get_title(self) -> str:
        return self._title


@tool(
    name="web_fetch",
    description=(
        "Fetch the content of a web page and extract readable text. "
        "Returns page title and text content. "
        "Use for reading documentation, API references, or public web pages."
    ),
    category="web",
    require_confirm=False,
)
def web_fetch(
    url: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    extract_text: bool = True,
) -> dict:
    """Fetch a web page and extract its content.

    Args:
        url: The URL to fetch.
        timeout_seconds: Request timeout in seconds.
        extract_text: If True, extract readable text from HTML.
                     If False, return raw HTML.

    Returns:
        Dict with 'title', 'content', 'url', and 'content_length'.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"URL must start with http:// or https://, got: {url}")

    _check_ssrf(url)

    req = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw_bytes = _streaming_read(resp)

            if not _is_decodable_content_type(content_type):
                return {
                    "title": "",
                    "content": f"[Non-text content: {content_type}]",
                    "url": url,
                    "content_length": len(raw_bytes),
                    "content_type": content_type,
                }

            raw = raw_bytes.decode(errors="replace")
    except HTTPError as e:
        return {
            "error": f"HTTP {e.code}: {e.reason}",
            "url": url,
            "content": "",
            "title": "",
        }
    except URLError as e:
        return {
            "error": f"URL error: {e.reason}",
            "url": url,
            "content": "",
            "title": "",
        }
    except TimeoutError:
        return {
            "error": f"Request timed out after {timeout_seconds}s",
            "url": url,
            "content": "",
            "title": "",
        }

    if not extract_text or "html" not in content_type.lower():
        content = raw[:_MAX_CONTENT_CHARS]
        return {
            "title": "",
            "content": content,
            "url": url,
            "content_length": len(raw),
        }

    extractor = _TextExtractor()
    extractor.feed(raw)
    text = extractor.get_text()

    if len(text) > _MAX_CONTENT_CHARS:
        text = text[:_MAX_CONTENT_CHARS] + "\n... (truncated)"

    return {
        "title": extractor.get_title(),
        "content": text,
        "url": url,
        "content_length": len(text),
    }
