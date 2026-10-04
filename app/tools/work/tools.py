"""Tools: Jira issues, Confluence pages and GitHub pull requests and issues.

Reading is safe. Creating an issue, commenting or moving status asks for your OK first, and
shows exactly what will be posted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.integrations.work import GitHub, Jira
from app.security.risk import RiskLevel
from app.tools.base import Input, Tool

# Reads that answer questions ("what's assigned to me?"); shared with the model when the
# user shares service results (Settings › Privacy).
READ_TOOLS = (
    "jira_my_issues",
    "jira_search",
    "jira_issue",
    "jira_projects",
    "confluence_search",
    "confluence_page",
    "github_reviews",
    "github_my_prs",
    "github_assigned",
    "github_read",
    "github_activity",
)
JIRA_READS = tuple(name for name in READ_TOOLS if not name.startswith("github"))
GITHUB_READS = tuple(name for name in READ_TOOLS if name.startswith("github"))


class SearchInput(Input):
    text: str = Field(default="", max_length=200, description="Words to look for")
    jql: str = Field(default="", max_length=500, description="A JQL query, if the user gave one")


class KeyInput(Input):
    key: str = Field(min_length=3, max_length=30, description="Issue key, e.g. PROJ-123")


class CreateInput(Input):
    project: str = Field(min_length=1, max_length=20, description="Project key, e.g. PROJ")
    summary: str = Field(min_length=3, max_length=250)
    description: str = Field(default="", max_length=8000)
    kind: Literal["Task", "Bug", "Story"] = "Task"
    assign_to_me: bool = True


class CommentInput(Input):
    key: str = Field(min_length=3, max_length=30)
    text: str = Field(min_length=1, max_length=8000)


class MoveInput(Input):
    key: str = Field(min_length=3, max_length=30)
    status: str = Field(min_length=2, max_length=60, description="e.g. In Progress, Done")


class PagesInput(Input):
    text: str = Field(min_length=2, max_length=200)


class PageInput(Input):
    page_id: str = Field(min_length=1, max_length=20, pattern=r"^\d+$")


class RepoItemInput(Input):
    repo: str = Field(min_length=3, max_length=140, description="owner/name")
    number: int = Field(ge=1)


class IssueInput(Input):
    repo: str = Field(min_length=3, max_length=140, description="owner/name")
    title: str = Field(min_length=3, max_length=250)
    body: str = Field(default="", max_length=8000)


class GitHubCommentInput(RepoItemInput):
    body: str = Field(min_length=1, max_length=8000)


class PullInput(Input):
    repo: str = Field(min_length=3, max_length=140, description="owner/name or just the name")


def _activity(data: dict) -> str:
    lines = []
    for r in data["repos"]:
        if not r["news"]:
            continue
        name = r["repo"].split("/")[-1]
        parts = []
        if r["commits"]:
            parts.append(f"{len(r['commits'])} new commits by {', '.join(r['pushers'][:3])}")
        if r["merged"]:
            parts.append(f"{len(r['merged'])} PRs merged")
        parts += [f"{rel['tag']} released" for rel in r["releases"]]
        if r["main_checks"] == "failing":
            parts.append(f"❌ checks failing on {r['branch']}")
        local = r.get("local") or {}
        if local.get("behind"):
            parts.append(f"your copy is {local['behind']} behind")
        lines.append(f"• {name}: " + "; ".join(parts))
    return "\n".join(lines) or "Nothing new in your repos."


def _issues(data: dict) -> str:
    items = data["issues"]
    if not items:
        return "No issues."
    lines = []
    for i in items[:15]:
        due = f" · due {i['due']}" if i.get("due") else ""
        prio = f" · {i['priority']}" if i.get("priority") else ""
        lines.append(f"• {i['key']} {i['summary']} — {i['status']}{prio}{due}")
    return "\n".join(lines)


def _prs(data: dict, empty: str) -> str:
    items = data["items"]
    if not items:
        return empty
    lines = []
    for p in items[:15]:
        checks = {"failing": " · ❌ checks failing", "running": " · checks running"}.get(
            p.get("checks", ""), ""
        )
        by = f" by {p['author']}" if p.get("author") else ""
        lines.append(f"• {p['repo']}#{p['number']} {p['title']}{by}{checks}")
    return "\n".join(lines)


class WorkController:
    def __init__(self, jira: Jira, github: GitHub, repos=None):
        self.jira, self.github, self.repos = jira, github, repos

    async def my_issues(self, _):
        return {"issues": await self.jira.mine(fresh=True)}

    async def search(self, args):
        if args.jql.strip():
            jql = args.jql.strip()
        elif args.text.strip():
            words = args.text.replace('"', " ").strip()
            jql = f'text ~ "{words}" ORDER BY updated DESC'
        else:
            raise ValueError("Say what to look for.")
        return {"issues": await self.jira.search(jql)}

    async def issue(self, args):
        return await self.jira.issue(args.key)

    async def projects(self, _):
        return {"projects": await self.jira.projects()}

    async def create(self, args):
        return await self.jira.create(
            args.project, args.summary, args.description, args.kind, args.assign_to_me
        )

    async def comment(self, args):
        return await self.jira.comment(args.key, args.text)

    async def move(self, args):
        return await self.jira.move(args.key, args.status)

    async def pages(self, args):
        return {"pages": await self.jira.pages(args.text)}

    async def page(self, args):
        return await self.jira.page(args.page_id)

    async def reviews(self, _):
        return {"items": await self.github.reviews(fresh=True)}

    async def my_prs(self, _):
        return {"items": await self.github.my_prs(fresh=True)}

    async def assigned(self, _):
        return {"items": await self.github.assigned(fresh=True)}

    async def read(self, args):
        return await self.github.read(args.repo, args.number)

    async def create_issue(self, args):
        return await self.github.create_issue(args.repo, args.title, args.body)

    async def github_comment(self, args):
        return await self.github.comment(args.repo, args.number, args.body)

    async def activity(self, _):
        return await self.repos.feed(fresh=True)

    async def pull(self, args):
        wanted = args.repo.lower().strip().removeprefix("https://github.com/").strip("/")
        copies = await self.repos.copies()
        matches = [name for name in copies if name == wanted or name.split("/")[-1] == wanted]
        if len(matches) != 1:
            raise ValueError(
                "Say which repo (owner/name)." if matches else "No copy of that repo on this Mac."
            )
        return await self.repos.pull(matches[0])


def register(registry, jira: Jira, github: GitHub, repos=None) -> None:
    c = WorkController(jira, github, repos)
    tools = [
        Tool(
            "jira_my_issues",
            "Jira issues assigned to the user that aren't done, most important first.",
            Input,
            RiskLevel.SAFE,
            c.my_issues,
            render=_issues,
        ),
        Tool(
            "jira_search",
            "Search Jira issues by words or JQL.",
            SearchInput,
            RiskLevel.SAFE,
            c.search,
            render=_issues,
        ),
        Tool(
            "jira_issue",
            "Read one Jira issue: description, status and recent comments.",
            KeyInput,
            RiskLevel.SAFE,
            c.issue,
            render=lambda d: f"{d['key']} {d['summary']} — {d['status']}\n{d['url']}",
        ),
        Tool(
            "jira_projects",
            "List Jira projects (keys and names), e.g. to create an issue.",
            Input,
            RiskLevel.SAFE,
            c.projects,
            render=lambda d: (
                "\n".join(f"• {p['key']} — {p['name']}" for p in d["projects"]) or "No projects."
            ),
        ),
        Tool(
            "jira_create",
            "Create a Jira issue (task, bug or story), e.g. from an email, a "
            "Slack message or what's on screen. Ask which project if unclear.",
            CreateInput,
            RiskLevel.CONFIRM,
            c.create,
            confirmation_message="Create this Jira issue?",
            render=lambda d: f"Created {d['key']}: {d['url']}",
        ),
        Tool(
            "jira_comment",
            "Add a comment to a Jira issue.",
            CommentInput,
            RiskLevel.CONFIRM,
            c.comment,
            confirmation_message="Post this comment on Jira?",
            render=lambda d: f"Commented on {d['key']}: {d['url']}",
        ),
        Tool(
            "jira_move",
            "Move a Jira issue to another status (e.g. In Progress, Done).",
            MoveInput,
            RiskLevel.CONFIRM,
            c.move,
            confirmation_message="Move this issue?",
            render=lambda d: f"{d['key']} is now {d['status']}.",
        ),
        Tool(
            "confluence_search",
            "Search Confluence pages by words.",
            PagesInput,
            RiskLevel.SAFE,
            c.pages,
            render=lambda d: (
                "\n".join(f"• {p['title']} ({p['space']}) {p['url']}" for p in d["pages"])
                or "No pages found."
            ),
        ),
        Tool(
            "confluence_page",
            "Read a Confluence page found with confluence_search.",
            PageInput,
            RiskLevel.SAFE,
            c.page,
            render=lambda d: f"{d['title']}\n{d['url']}",
        ),
        Tool(
            "github_reviews",
            "GitHub pull requests waiting for the user's review.",
            Input,
            RiskLevel.SAFE,
            c.reviews,
            render=lambda d: _prs(d, "No pull requests are waiting for your review."),
        ),
        Tool(
            "github_my_prs",
            "The user's open GitHub pull requests and whether their checks pass.",
            Input,
            RiskLevel.SAFE,
            c.my_prs,
            render=lambda d: _prs(d, "You have no open pull requests."),
        ),
        Tool(
            "github_assigned",
            "Open GitHub issues assigned to the user.",
            Input,
            RiskLevel.SAFE,
            c.assigned,
            render=lambda d: _prs(d, "No open issues are assigned to you."),
        ),
        Tool(
            "github_read",
            "Read a GitHub issue or pull request and its recent comments.",
            RepoItemInput,
            RiskLevel.SAFE,
            c.read,
            render=lambda d: f"{d['repo']}#{d['number']} {d['title']} — {d['state']}\n{d['url']}",
        ),
        Tool(
            "github_create_issue",
            "Create a GitHub issue in a repository.",
            IssueInput,
            RiskLevel.CONFIRM,
            c.create_issue,
            confirmation_message="Create this GitHub issue?",
            render=lambda d: f"Created {d['repo']}#{d['number']}: {d['url']}",
        ),
        Tool(
            "github_comment",
            "Comment on a GitHub issue or pull request.",
            GitHubCommentInput,
            RiskLevel.CONFIRM,
            c.github_comment,
            confirmation_message="Post this comment on GitHub?",
            render=lambda d: f"Commented: {d['url']}",
        ),
    ]
    if repos is not None:
        tools += [
            Tool(
                "github_activity",
                "What changed in the user's GitHub repos: teammates' new "
                "commits on the main branch, releases, merged pull requests, failing checks on "
                "main, and whether the user's copies on this Mac are behind.",
                Input,
                RiskLevel.SAFE,
                c.activity,
                render=_activity,
            ),
            Tool(
                "git_pull",
                "Update the user's copy of a repo on this Mac from GitHub (fast-"
                "forward only, only when it has no uncommitted changes).",
                PullInput,
                RiskLevel.CONFIRM,
                c.pull,
                confirmation_message="Pull the latest code?",
                render=lambda d: d["message"],
            ),
        ]
    for tool in tools:
        registry.register(tool)
