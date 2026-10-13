"""Watching web pages: only public pages, only their words, what changed. No real network."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.assistant import webpage
from app.assistant.watchers import Watchers
from app.workflows.watches import Watch


@pytest.fixture
def dns(monkeypatch):
    table = {"shop.example": "93.184.216.34", "inside.example": "192.168.1.20"}

    def lookup(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr(webpage.socket, "getaddrinfo", lookup)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://127.0.0.1/admin",
        "https://inside.example/",
        "file:///etc/hosts",
        "ftp://shop.example/",
        "https://printer.local/",
    ],
)
def test_only_public_web_pages(dns, url):
    with pytest.raises(webpage.PageError):
        webpage.check_address(url)


def test_public_pages_are_fine(dns):
    assert webpage.check_address(" https://shop.example/flights ") == "https://shop.example/flights"


PAGE = """<!doctype html><html><head><title>Flights to Kigali</title>
<script>var x = 'secret';</script><style>.a{}</style></head>
<body><h1>Brussels → Kigali</h1><p>From <b>$520</b> per person</p>
<div>Book by Friday</div></body></html>"""


async def test_reads_only_the_words(dns):
    def handler(request):
        return httpx.Response(200, html=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await webpage.fetch("https://shop.example/flights", client)
    assert page["title"] == "Flights to Kigali"
    assert "From $520 per person" in page["text"] and "secret" not in page["text"]
    assert len(page["hash"]) == 24


async def test_a_redirect_to_your_network_is_refused(dns):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://inside.example/router"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(webpage.PageError, match="public web pages"):
            await webpage.fetch("https://shop.example/go", client)


async def test_script_only_pages_are_explained(dns):
    def handler(request):
        return httpx.Response(
            200, html="<html><body><div id=root></div><script>app()</script></body></html>"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(webpage.PageError, match="built by scripts"):
            await webpage.fetch("https://shop.example/app", client)


def test_what_changed():
    assert (
        webpage.what_changed("Jobs\nPython dev", "Jobs\nPython dev\nRust dev\nGo dev")
        == "New: Rust dev; Go dev"
    )
    assert webpage.what_changed("a", "a") == "Something on the page changed."


def watch(condition="", once=False):
    return Watch(
        1,
        "web",
        "shop.example: x",
        "Flight price",
        [],
        False,
        None,
        None,
        0,
        target={"url": "https://shop.example/flights", "condition": condition},
        once=once,
    )


def page(text):
    return {
        "url": "https://shop.example/flights",
        "title": "Flights",
        "text": text,
        "hash": webpage.hashlib.sha256(text.encode()).hexdigest()[:24],
    }


async def test_changes_and_conditions():
    watchers = Watchers()
    w = watch()
    first = page("From $520\nBook by Friday")
    before = watchers.remember(w, first)
    assert await watchers.events(w, None, first) == []  # The first look only learns.
    assert await watchers.events(w, before, first) == []  # Same words: nothing.
    later = page("From $520\nBook by Friday\nDirect flights in May")
    [event] = await watchers.events(w, before, later)
    assert event["headline"] == "Flights changed"
    assert event["detail"] == "New: Direct flights in May" and event["keep"] is False

    llm = SimpleNamespace(
        complete=AsyncMock(return_value=json.dumps({"met": True, "summary": "Now $389"}))
    )
    watchers = Watchers(llm)
    w = watch("the price drops below $400", once=True)
    before = watchers.remember(w, first)
    cheaper = page("From $389\nBook by Friday")
    [event] = await watchers.events(w, before, cheaper)
    assert event["detail"] == "Now $389"
    assert "untrusted" in llm.complete.await_args.args[0]
    assert await watchers.events(w, watchers.remember(w, cheaper), cheaper) == []
    assert llm.complete.await_count == 1  # No model call when nothing changed.
