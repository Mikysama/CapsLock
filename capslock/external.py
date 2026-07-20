"""Web content validation shared by the async action handler."""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

from .policy import PolicyError

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_INJECTION = re.compile(
    r"(?i)(ignore (?:all )?(?:previous )?instructions|system prompt|you are now|tool[_ ]?call|assistant message)"
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def extract_text(body: str) -> str:
    parser = _TextExtractor()
    parser.feed(body)
    return " ".join(" ".join(parser.parts).split())


def is_suspicious(text: str) -> bool:
    return bool(_INJECTION.search(text))


def validate_public_url(url: str, resolver=socket.getaddrinfo) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PolicyError("URL must use http or https with a hostname")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise PolicyError("localhost URLs are not allowed")
    try:
        records = resolver(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise PolicyError(f"URL hostname cannot be resolved: {hostname}") from exc
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise PolicyError("private or non-public URL addresses are not allowed")
    return url
