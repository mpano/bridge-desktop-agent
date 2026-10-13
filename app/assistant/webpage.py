"""Reading a public web page for a watcher: politely, safely, and only its words.

Only http(s) pages on the public internet: addresses on this Mac or your local network are
refused (also after a redirect), so a watcher can't be used to poke at your router or other
machines. One request, at most 2 MB, a plain user agent; scripts and styles are dropped and
the readable text kept.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
SNAPSHOT = 8000  # Characters of the page kept to say what changed.
AGENT = "Mozilla/5.0 (Macintosh) Bridge-PageWatcher/1.0"


class PageError(ValueError):
    """Safe to show to the user."""


class _Readable(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BREAK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "tr", "br", "div", "section", "article", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def readable(html: str) -> tuple[str, str]:
    parser = _Readable()
    try:
        parser.feed(html)
    except Exception:
        pass
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    text = "\n".join(line for line in lines if line)
    return " ".join(parser.title.split())[:200], text


def check_address(url: str) -> str:
    """The URL, if it's a public http(s) page; otherwise a PageError."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PageError("Use a web address that starts with https://")
    host = parts.hostname
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        raise PageError("Bridge only watches public web pages, not ones on your network.")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise PageError(f"Couldn't find {host}. Check the address.") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise PageError("Bridge only watches public web pages, not ones on your network.")
    return url.strip()


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> dict:
    """{"url", "title", "text", "hash"} for a public page."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await asyncio.to_thread(check_address, current)
            async with client.stream(
                "GET", current, headers={"User-Agent": AGENT, "Accept": "text/html,*/*"}
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise PageError("The page redirected nowhere.")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise PageError(f"The page answered {response.status_code}.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                encoding = response.encoding or "utf-8"
                kind = response.headers.get("content-type", "")
                break
        else:
            raise PageError("The page redirected too many times.")
    except httpx.HTTPError:
        raise PageError("Couldn't reach the page right now.") from None
    finally:
        if own:
            await client.aclose()
    raw = bytes(body).decode(encoding, errors="replace")
    if "html" in kind or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, text = readable(raw)
    else:
        title, text = "", re.sub(r"[ \t]+", " ", raw)
    if len(text) < 40:
        raise PageError(
            "This page shows almost no text without a browser (it's built by scripts), so "
            "Bridge can't read it."
        )
    return {
        "url": current,
        "title": title,
        "text": text,
        "hash": hashlib.sha256(text.encode()).hexdigest()[:24],
    }


def what_changed(before: str, after: str, limit: int = 3) -> str:
    """A short "what's new on the page" from two snapshots."""
    added = [
        line[1:].strip()
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 3
    ]
    if not added:
        return "Something on the page changed."
    shown = "; ".join(a[:90] for a in added[:limit])
    more = f" (+{len(added) - limit} more)" if len(added) > limit else ""
    return f"New: {shown}{more}"
