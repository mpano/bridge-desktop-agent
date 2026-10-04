"""What's happening in the GitHub repos you work on, and keeping your copies up to date.

Bridge finds your git copies on this Mac (projects you told it about, and a shallow look
through your home folder), matches them to GitHub, and watches those repos plus the ones you
pushed to recently: teammates' new commits on the main branch, releases, merged and new pull
requests, and failing checks on main. It tells you once about each, and offers to pull when
your copy is behind.

Pull is careful: only a fast-forward of a clean copy on its main branch, never a merge, and
only when you ask. `git fetch` only updates what your copy knows about the remote.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from app.integrations.tokens import enc

log = logging.getLogger(__name__)
GIT = "/usr/bin/git"
SCAN_SECONDS = 3600
CHECK_SECONDS = 15 * 60
MAX_REPOS = 12
# Not looked through: system and media folders, folders macOS protects (looking would make it
# ask you for permission), cloud drives, and dependency caches.
SKIP = {
    "Library",
    "Applications",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "Downloads",
    "Desktop",
    "Documents",
    "Dropbox",
    "Google Drive",
    "OneDrive",
    "iCloud Drive",
    "Creative Cloud Files",
    "node_modules",
    "venv",
    "vendor",
    "dist",
    "build",
    "target",
    "Pods",
    "pkg",
    "bin",
}
SCAN_BUDGET = 4.0  # Seconds: a slow disk never holds Bridge up.
REMOTE = re.compile(
    r"github\.com[:/](?P<owner>[A-Za-z0-9-]{1,39})/(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)


def github_name(config: Path) -> str | None:
    """owner/name of a copy's GitHub origin, read from its .git/config."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(config, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    url = parser.get('remote "origin"', "url", fallback="")
    match = REMOTE.search(url.strip())
    return f"{match['owner']}/{match['name']}".lower() if match else None


def find_copies(home: Path, extra: list[str] = (), depth: int = 3) -> dict[str, str]:
    """GitHub repo → local folder, for git copies under your home folder (not too deep)."""
    found: dict[str, str] = {}
    deadline = time.monotonic() + SCAN_BUDGET

    def look(folder: Path, level: int):
        if time.monotonic() > deadline:
            return
        config = folder / ".git" / "config"
        if config.is_file():
            name = github_name(config)
            if name and name not in found:
                found[name] = str(folder)
            return  # Don't look for repos inside a repo.
        if level >= depth:
            return
        try:
            with os.scandir(folder) as listing:
                entries = []
                for entry in listing:
                    entries.append(entry)
                    if len(entries) >= 400:
                        break
        except OSError:
            return
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.name.startswith(".") or entry.name in SKIP:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    look(Path(entry.path), level + 1)
            except OSError:
                continue

    for path in extra:
        candidate = Path(path).expanduser()
        if (candidate / ".git" / "config").is_file():
            look(candidate, depth)
    look(home, 0)
    return found


async def git(path: str, *args: str, timeout: float = 30) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        GIT,
        "-C",
        path,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",  # Never wait for a password.
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            "LC_ALL": "C",
        },
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        return 124, "git took too long"
    return process.returncode, out.decode(errors="replace").strip()


def used_at(path: str) -> float:
    """When a copy was last used here (a commit, checkout or fetch)."""
    git_dir = Path(path) / ".git"
    times = []
    for name in ("index", "HEAD", "FETCH_HEAD", "ORIG_HEAD"):
        try:
            times.append((git_dir / name).stat().st_mtime)
        except OSError:
            continue
    return max(times, default=0.0)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(text: str | None) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class RepoWatcher:
    def __init__(self, path, github, *, projects=None, home: Path | None = None, clock=time.time):
        self.github, self.projects, self.clock = github, projects, clock
        self.home = home or Path.home()
        self.path = str(path)
        self.notify = None
        self._copies: tuple[float, dict] | None = None
        self._feed: tuple[float, dict] | None = None
        self._last_check = 0.0
        self.lock = asyncio.Lock()
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS repo_seen (repo TEXT PRIMARY KEY, since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS repo_told (event TEXT PRIMARY KEY, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS repo_contributor (
                    repo TEXT PRIMARY KEY, yes INTEGER NOT NULL, at REAL NOT NULL);
                """
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=5)

    def _since(self, repo: str) -> float:
        """News since you last marked this repo as seen (at most a week back)."""
        with self._db() as db:
            row = db.execute("SELECT since FROM repo_seen WHERE repo = ?", (repo,)).fetchone()
        floor = self.clock() - 7 * 86400
        return max(row[0], floor) if row else self.clock() - 3 * 86400

    def mark_seen(self, repo: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO repo_seen VALUES (?, ?) ON CONFLICT(repo) DO UPDATE SET since = ?",
                (repo, self.clock(), self.clock()),
            )
        self._feed = None

    def _tell_once(self, event: str) -> bool:
        with self._db() as db:
            db.execute("DELETE FROM repo_told WHERE at < ?", (self.clock() - 30 * 86400,))
            return (
                db.execute(
                    "INSERT OR IGNORE INTO repo_told VALUES (?, ?)", (event, self.clock())
                ).rowcount
                > 0
            )

    # Which repos ----------------------------------------------------------------------------

    async def copies(self) -> dict[str, str]:
        if self._copies and self.clock() - self._copies[0] < SCAN_SECONDS:
            return self._copies[1]
        extra = []
        if self.projects is not None:
            try:
                extra = [item.value for item in self.projects.projects()]
            except Exception:
                extra = []
        found = await asyncio.to_thread(find_copies, self.home, extra)
        self._copies = (self.clock(), found)
        return found

    async def contributed(self, repo: str, me: str) -> bool:
        """Whether you have commits in this repo (remembered for a day)."""
        key = repo.lower()
        with self._db() as db:
            row = db.execute(
                "SELECT yes, at FROM repo_contributor WHERE repo = ?", (key,)
            ).fetchone()
        if row and self.clock() - row[1] < 86400:
            return bool(row[0])
        try:
            found = await self.github.c.request(
                "github", "GET", f"repos/{repo}/commits", params={"author": me, "per_page": 1}
            )
        except Exception:
            return bool(row and row[0])  # Unknown right now: keep the last answer.
        yes = bool(found)
        with self._db() as db:
            db.execute(
                "INSERT INTO repo_contributor VALUES (?, ?, ?) ON CONFLICT(repo) "
                "DO UPDATE SET yes = excluded.yes, at = excluded.at",
                (key, int(yes), self.clock()),
            )
        return yes

    async def repos(self) -> dict[str, dict]:
        """The repos that matter now, among those you've committed to: ones pushed to on GitHub
        this month, then copies you used on this Mac this month (most recent first)."""
        listed = await self.github.c.request(
            "github",
            "GET",
            "user/repos",
            params={
                "sort": "pushed",
                "per_page": 30,
                "affiliation": "owner,collaborator,organization_member",
            },
        )
        cutoff = self.clock() - 30 * 86400
        local = await self.copies()
        recent = [r for r in listed if when(r.get("pushed_at")) >= cutoff and not r.get("archived")]
        recent.sort(key=lambda r: when(r.get("pushed_at")), reverse=True)
        chosen: dict[str, dict] = {}
        for r in recent:
            key = r["full_name"].lower()
            chosen[key] = {
                "name": r["full_name"],
                "branch": r.get("default_branch") or "main",
                "path": local.get(key),
            }
        used = sorted(
            ((used_at(path), name, path) for name, path in local.items() if name not in chosen),
            reverse=True,
        )
        for at, name, path in used:
            if len(chosen) >= MAX_REPOS * 2 or at < cutoff:
                break
            chosen[name] = {"name": name, "branch": None, "path": path}
        # Only repos you contribute to: an organization's other repos don't belong here.
        me = await self.github.c.me("github")
        if me:
            gate = asyncio.Semaphore(6)

            async def keep(name, info):
                async with gate:
                    return name if await self.contributed(info["name"], me) else None

            kept = set(await asyncio.gather(*(keep(n, i) for n, i in chosen.items())))
            chosen = {name: info for name, info in chosen.items() if name in kept}
        return dict(list(chosen.items())[:MAX_REPOS])

    # What happened ---------------------------------------------------------------------------

    async def _activity(self, key: str, info: dict, me: str) -> dict:
        repo = info["name"]
        since = self._since(key)
        r = self.github.c.request
        if info.get("branch") is None:
            meta = await r("github", "GET", f"repos/{repo}")
            info["branch"] = meta.get("default_branch") or "main"
            info["name"] = repo = meta.get("full_name", repo)
        branch = info["branch"]
        commits, releases, merged = await asyncio.gather(
            r(
                "github",
                "GET",
                f"repos/{repo}/commits",
                params={"sha": branch, "since": iso(since), "per_page": 30},
            ),
            r("github", "GET", f"repos/{repo}/releases", params={"per_page": 3}),
            r(
                "github",
                "GET",
                "search/issues",
                params={
                    "q": f"repo:{repo} is:pr is:merged merged:>={iso(since)[:10]}",
                    "per_page": 10,
                },
            ),
            return_exceptions=True,
        )
        theirs = []
        for commit in commits if isinstance(commits, list) else []:
            login = (commit.get("author") or {}).get("login") or ""
            if login.lower() == me.lower():
                continue
            theirs.append(
                {
                    "sha": commit.get("sha", "")[:7],
                    "by": login
                    or ((commit.get("commit") or {}).get("author") or {}).get("name", ""),
                    "message": ((commit.get("commit") or {}).get("message") or "").split("\n")[0][
                        :120
                    ],
                    "at": ((commit.get("commit") or {}).get("committer") or {}).get("date"),
                }
            )
        new_releases = [
            {
                "tag": rel.get("tag_name"),
                "name": rel.get("name") or rel.get("tag_name"),
                "url": rel.get("html_url"),
                "at": rel.get("published_at"),
                "by": (rel.get("author") or {}).get("login", ""),
            }
            for rel in (releases if isinstance(releases, list) else [])
            if when(rel.get("published_at")) >= since and not rel.get("draft")
        ]
        merged_prs = [
            {
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "by": (pr.get("user") or {}).get("login", ""),
                "url": pr.get("html_url"),
            }
            for pr in (merged.get("items", []) if isinstance(merged, dict) else [])
        ]
        main_checks = "unknown"
        try:
            head = await r("github", "GET", f"repos/{repo}/commits/{enc(branch)}")
            runs = await r(
                "github",
                "GET",
                f"repos/{repo}/commits/{enc(head['sha'])}/check-runs",
                params={"per_page": 50},
            )
            results = [x.get("conclusion") or x.get("status") for x in runs.get("check_runs", [])]
            failed = any(x in {"failure", "timed_out", "action_required"} for x in results)
            main_checks = "failing" if failed else "passing" if results else "none"
            info["head"] = head.get("sha", "")[:7]
        except Exception:
            pass
        return {
            "repo": repo,
            "key": key,
            "branch": branch,
            "url": f"https://github.com/{repo}",
            "commits": theirs,
            "pushers": sorted({c["by"] for c in theirs if c["by"]}),
            "releases": new_releases,
            "merged": merged_prs,
            "main_checks": main_checks,
            "head": info.get("head"),
            "path": info.get("path"),
        }

    async def _local(self, path: str, branch: str) -> dict:
        """Your copy: its branch, whether it has changes, and how far behind GitHub it is."""
        code, current = await git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            return {"ok": False}
        _, status = await git(path, "status", "--porcelain", "--untracked-files=no")
        await git(path, "fetch", "--quiet", "origin", branch, timeout=40)
        code, counts = await git(
            path, "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"
        )
        ahead = behind = 0
        if code == 0 and counts.split():
            ahead, behind = (int(x) for x in counts.split()[:2])
        return {
            "ok": True,
            "folder": path.replace(str(self.home), "~", 1),
            "branch": current,
            "on_main": current == branch,
            "changes": bool(status.strip()),
            "ahead": ahead,
            "behind": behind,
            "can_pull": current == branch and not status.strip() and behind > 0 and ahead == 0,
        }

    async def feed(self, *, fresh: bool = False) -> dict:
        async with self.lock:
            if not fresh and self._feed and self.clock() - self._feed[0] < 5 * 60:
                return self._feed[1]
            me = await self.github.c.me("github")
            repos = await self.repos()
            gate = asyncio.Semaphore(4)

            async def one(key, info):
                async with gate:
                    try:
                        item = await self._activity(key, info, me)
                    except Exception as exc:
                        log.info("Repo activity for %s failed: %s", key, type(exc).__name__)
                        return None
                    if info.get("path"):
                        try:
                            item["local"] = await self._local(info["path"], item["branch"])
                        except Exception:
                            item["local"] = {"ok": False}
                    return item

            items = [i for i in await asyncio.gather(*(one(k, v) for k, v in repos.items())) if i]
            for item in items:
                item["news"] = bool(
                    item["commits"]
                    or item["releases"]
                    or item["merged"]
                    or item["main_checks"] == "failing"
                    or (item.get("local") or {}).get("behind")
                )
            items.sort(key=lambda i: (not i["news"], i["repo"].lower()))
            result = {"repos": items, "checked_at": self.clock()}
            self._feed = (self.clock(), result)
            return result

    # Pulling -------------------------------------------------------------------------------

    async def pull(self, repo: str) -> dict:
        key = repo.lower().strip()
        path = (await self.copies()).get(key)
        if not path:
            raise ValueError("Bridge didn't find a copy of that repo on this Mac.")
        _, branch_ref = await git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        main = branch_ref.removeprefix("origin/") if branch_ref.startswith("origin/") else None
        local = await self._local(path, main or "main")
        if not local["ok"]:
            raise ValueError("That folder isn't a working git copy.")
        if local["changes"]:
            raise ValueError("Your copy has uncommitted changes. Commit or stash them first.")
        if main and not local["on_main"]:
            raise ValueError(f"Your copy is on {local['branch']}, not {main}. Switch first.")
        if local["ahead"]:
            raise ValueError("Your copy has commits GitHub doesn't. Pull it yourself to merge.")
        if not local["behind"]:
            return {"repo": repo, "pulled": 0, "message": "Already up to date."}
        code, out = await git(path, "pull", "--ff-only", "--quiet", timeout=90)
        if code != 0:
            raise ValueError(
                "Pull didn't finish: " + (out.splitlines()[-1] if out else "git failed")[:200]
            )
        self._feed = None
        count = local["behind"]
        plural = "s" if count != 1 else ""
        return {
            "repo": repo,
            "pulled": count,
            "folder": local["folder"],
            "message": f"Pulled {count} commit{plural} into {local['folder']}.",
        }

    # Telling you (every 15 minutes, from the proactive loop) ------------------------------

    async def check(self) -> int:
        if self.notify is None or self.clock() - self._last_check < CHECK_SECONDS:
            return 0
        self._last_check = self.clock()
        if not await self.github.connected():
            return 0
        try:
            feed = await self.feed(fresh=True)
        except Exception:
            return 0
        told = 0
        for item in feed["repos"]:
            short = item["repo"].split("/")[-1]
            local = item.get("local") or {}
            if item["commits"] and item["pushers"]:
                last = item["commits"][0]["sha"]
                if self._tell_once(f"push:{item['key']}:{last}"):
                    who = ", ".join(item["pushers"][:2])
                    n = len(item["commits"])
                    body = f"{n} new commit{'s' if n != 1 else ''} on {item['branch']}"
                    if local.get("can_pull"):
                        body += f". Your copy is {local['behind']} behind — pull?"
                    await self.notify(f"{who} pushed to {short}", body, "today")
                    told += 1
            for release in item["releases"]:
                if self._tell_once(f"release:{item['key']}:{release['tag']}"):
                    await self.notify(
                        f"{short} {release['tag']} released", release["name"] or "", "today"
                    )
                    told += 1
            if item["main_checks"] == "failing" and self._tell_once(
                f"broken:{item['key']}:{item.get('head') or ''}"
            ):
                await self.notify(
                    f"Checks failing on {short} {item['branch']}",
                    "The latest commit on the main branch has failing checks.",
                    "today",
                )
                told += 1
        return told
