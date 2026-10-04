"""Your repos: teammates' pushes, releases, broken main, and pulling your copy safely."""

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from app.integrations.repos import RepoWatcher, find_copies, github_name
from app.integrations.tokens import MemoryTokens, TokenConnections
from app.integrations.work import GitHub
from app.tools.work.tools import _activity

NOW = 1_790_000_000.0  # 2026-09-21


def run(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def commit(folder: Path, name: str):
    (folder / name).write_text(name)
    run("add", name, cwd=folder)
    run("commit", "-m", f"Add {name}", cwd=folder)


def test_github_names_and_finding_copies(tmp_path):
    def repo(folder: Path, url: str):
        (folder / ".git").mkdir(parents=True)
        (folder / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')

    repo(tmp_path / "work" / "web", "git@github.com:Acme/Web.git")
    repo(tmp_path / "code" / "api", "https://github.com/acme/api")
    repo(
        tmp_path / "code" / "api" / "vendor-copy", "https://github.com/other/inside"
    )  # Inside a repo.
    repo(tmp_path / "Library" / "x", "https://github.com/acme/hidden")  # Skipped folder.
    repo(tmp_path / "a" / "b" / "c" / "d", "https://github.com/acme/too-deep")
    repo(tmp_path / "gitlab", "git@gitlab.com:acme/nope.git")
    assert find_copies(tmp_path) == {
        "acme/api": str(tmp_path / "code" / "api"),
        "acme/web": str(tmp_path / "work" / "web"),
    }
    deep = tmp_path / "a" / "b" / "c" / "d"
    assert find_copies(tmp_path, [str(deep)])["acme/too-deep"] == str(deep)  # A project you named.
    config = tmp_path / "work" / "web" / ".git" / "config"
    assert github_name(config) == "acme/web"


@pytest.fixture
def copies(tmp_path):
    """A GitHub stand-in (a bare repo), your copy, and a teammate's copy."""
    bare = tmp_path / "web.git"
    run("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    run("clone", str(bare), str(seed))
    run("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
    commit(seed, "readme")
    run("push", "origin", "main", cwd=seed)
    mine = tmp_path / "mine"
    run("clone", str(bare), str(mine))
    run("remote", "set-url", "origin", "git@github.com:acme/web.git", cwd=mine)
    run("config", f"url.{bare}.insteadOf", "git@github.com:acme/web.git", cwd=mine)
    commit(seed, "feature-1")
    commit(seed, "feature-2")
    run("push", "origin", "main", cwd=seed)
    return mine


def watcher(tmp_path, transport=None, copies_found=None):
    http = httpx.AsyncClient(transport=transport) if transport else None
    store = MemoryTokens()
    store.data = {
        "github": {
            "method": "token",
            "token": "t",
            "identity": "me",
            "user_id": "me",
            "actions": True,
        }
    }
    w = RepoWatcher(tmp_path / "db", GitHub(TokenConnections(store, http=http)), clock=lambda: NOW)
    w._copies = (NOW, copies_found or {})
    return w


def test_pull_is_a_fast_forward_of_a_clean_copy_only(tmp_path, copies):
    w = watcher(tmp_path, copies_found={"acme/web": str(copies)})
    local = asyncio.run(w._local(str(copies), "main"))
    assert local | {"folder": ""} == {
        "ok": True,
        "folder": "",
        "branch": "main",
        "on_main": True,
        "changes": False,
        "ahead": 0,
        "behind": 2,
        "can_pull": True,
    }
    (copies / "readme").write_text("my edit")
    with pytest.raises(ValueError, match="uncommitted changes"):
        asyncio.run(w.pull("acme/web"))
    run("checkout", "readme", cwd=copies)
    result = asyncio.run(w.pull("acme/web"))
    assert result["pulled"] == 2 and (copies / "feature-2").exists()
    assert asyncio.run(w.pull("acme/web"))["message"] == "Already up to date."
    with pytest.raises(ValueError, match="didn't find a copy"):
        asyncio.run(w.pull("acme/elsewhere"))


def test_pull_never_merges_your_own_commits(tmp_path, copies):
    commit(copies, "mine-only")
    w = watcher(tmp_path, copies_found={"acme/web": str(copies)})
    with pytest.raises(ValueError, match="commits GitHub doesn't"):
        asyncio.run(w.pull("acme/web"))


def fake_github(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/user/repos":
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "acme/web",
                    "default_branch": "main",
                    "pushed_at": "2026-09-20T10:00:00Z",
                },
                {
                    "full_name": "acme/old",
                    "default_branch": "main",
                    "pushed_at": "2025-01-01T10:00:00Z",
                },
            ],
        )
    if path == "/repos/acme/web/commits" and "sha" in request.url.params:
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "aaaaaaa1",
                    "author": {"login": "ana"},
                    "commit": {
                        "message": "Fix pricing\n\nbody",
                        "committer": {"date": "2026-09-20T09:00:00Z"},
                    },
                },
                {
                    "sha": "bbbbbbb2",
                    "author": {"login": "me"},
                    "commit": {"message": "Mine", "committer": {}},
                },
            ],
        )
    if path == "/repos/acme/web/releases":
        return httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v1.4.0",
                    "name": "Pricing",
                    "html_url": "https://github.com/acme/web/releases/v1.4.0",
                    "published_at": "2026-09-20T12:00:00Z",
                    "author": {"login": "sam"},
                }
            ],
        )
    if path == "/search/issues":
        assert "repo:acme/web is:pr is:merged" in request.url.params["q"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {"number": 9, "title": "New plans", "user": {"login": "ana"}, "html_url": "u"}
                ]
            },
        )
    if path == "/repos/acme/web/commits/main":
        return httpx.Response(200, json={"sha": "aaaaaaa1"})
    if path == "/repos/acme/web/commits/aaaaaaa1/check-runs":
        return httpx.Response(200, json={"check_runs": [{"conclusion": "failure"}]})
    return httpx.Response(404, json={})


def test_feed_and_notifications_once(tmp_path):
    w = watcher(tmp_path, httpx.MockTransport(fake_github))
    notes = []

    async def notify(title, body, view):
        notes.append(title)

    async def scenario():  # One event loop, as in Bridge.
        feed = await w.feed()
        w.notify = notify
        first = await w.check()
        w._last_check = 0
        second = await w.check()
        return feed, first, second

    feed, first, second = asyncio.run(scenario())
    [repo] = feed["repos"]  # acme/old wasn't pushed to for months.
    assert repo["pushers"] == ["ana"] and repo["commits"][0]["message"] == "Fix pricing"
    assert repo["releases"][0]["tag"] == "v1.4.0" and repo["merged"][0]["title"] == "New plans"
    assert repo["main_checks"] == "failing" and repo["news"]
    assert "ana" in _activity(feed) and "v1.4.0 released" in _activity(feed)
    assert first == 3
    assert notes == ["ana pushed to web", "web v1.4.0 released", "Checks failing on web main"]
    assert second == 0  # Each thing is told once.
    w.mark_seen("acme/web")
    assert w._since("acme/web") == NOW


def test_phone_can_look_but_not_pull():
    from app.phone.access import phone_may_use

    assert phone_may_use("GET", "/api/v1/repos")
    assert not phone_may_use("POST", "/api/v1/repos/pull")
