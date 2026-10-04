"use strict";

// The Work card on Today: Jira issues assigned to you, pull requests waiting for your review,
// and your own pull requests with their checks. Links open in your browser.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let loading = false;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function link(text, href, className) {
    const element = node("a", text, className);
    element.href = href;
    element.target = "_blank";
    element.rel = "noopener noreferrer";
    return element;
  }

  function row(main, meta, href, badge, tone) {
    const li = node("li", undefined, "work-item");
    const words = node("div", undefined, "work-words");
    words.append(link(main, href, "work-title"), node("span", meta, "work-meta"));
    li.append(words);
    if (badge) li.append(node("span", badge, `work-badge ${tone || ""}`));
    return li;
  }

  function section(title, items) {
    if (!items.length) return null;
    const box = node("div", undefined, "work-section");
    const list = node("ul", undefined, "plain work-list");
    list.append(...items);
    box.append(node("p", title, "work-head"), list);
    return box;
  }

  const today = () => new Date().toISOString().slice(0, 10);

  async function refresh(fresh = false) {
    if (loading) return;
    loading = true;
    let data;
    try {
      data = await ui().request(`/api/v1/work${fresh ? "?fresh=true" : ""}`);
    } catch {
      $("work-card").hidden = true;
      loading = false;
      return;
    }
    loading = false;
    const {jira, github} = data;
    $("work-card").hidden = !jira.connected && !github.connected;
    const body = $("work-body");
    body.replaceChildren();
    if (github.connected) {
      const reviews = section("Waiting for your review", (github.reviews || []).map((pr) =>
        row(pr.title, `${pr.repo}#${pr.number} · ${pr.author}`, pr.url, pr.draft ? "draft" : "", "")));
      const failing = (github.mine || []).filter((pr) => pr.checks === "failing");
      const mine = section("Your pull requests", (github.mine || []).slice(0, 4).map((pr) =>
        row(pr.title, `${pr.repo}#${pr.number}`, pr.url,
          {failing: "checks failing", running: "running", passing: "✓ passing"}[pr.checks] || "",
          pr.checks === "failing" ? "bad" : pr.checks === "passing" ? "good" : "")));
      if (reviews) body.append(reviews);
      if (failing.length && !reviews) body.append(node("p", `${failing.length} of your pull requests ${failing.length === 1 ? "has" : "have"} failing checks.`, "work-alert"));
      if (mine) body.append(mine);
      if (!reviews && !mine) body.append(node("p", "Nothing waiting on GitHub.", "empty"));
    }
    if (jira.connected) {
      const issues = section("Jira · assigned to you", (jira.issues || []).slice(0, 6).map((issue) => {
        const late = issue.due && issue.due < today();
        const badge = late ? "overdue" : issue.due === today() ? "due today" : issue.status;
        return row(`${issue.key} ${issue.summary}`, [issue.priority, issue.type].filter(Boolean).join(" · "), issue.url, badge, late ? "bad" : "");
      }));
      body.append(issues || node("p", "No open Jira issues assigned to you.", "empty"));
    }
    for (const error of [jira.error, github.error].filter(Boolean)) body.append(node("p", error, "notice-box bad"));
  }

  // Your repos: teammates' pushes, releases, merged PRs, broken main, and your copy here.
  let reposLoading = false;

  async function repos(fresh = false) {
    if (reposLoading) return;
    reposLoading = true;
    const box = $("work-repos");
    if (!box.children.length) box.replaceChildren(node("p", "Checking your repos…", "work-meta"));
    let data;
    try {
      data = await ui().request(`/api/v1/repos${fresh ? "?fresh=true" : ""}`);
    } catch {
      box.replaceChildren();
      reposLoading = false;
      return;
    }
    reposLoading = false;
    box.replaceChildren();
    if (!data.connected) return;
    if (data.error) box.append(node("p", data.error, "notice-box bad"));
    const news = (data.repos || []).filter((repo) => repo.news);
    if (!news.length) {
      if (data.repos && data.repos.length) box.append(node("p", `Nothing new in your ${data.repos.length} repos.`, "work-meta"));
      return;
    }
    const list = node("ul", undefined, "plain work-list");
    for (const repo of news.slice(0, 6)) list.append(repoRow(repo));
    const section = node("div", undefined, "work-section");
    section.append(node("p", "Your repos", "work-head"), list);
    box.append(section);
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  function repoRow(repo) {
    const li = node("li", undefined, "repo-item");
    const top = node("div", undefined, "repo-top");
    top.append(link(repo.repo.split("/")[1], repo.url, "work-title"));
    if (repo.main_checks === "failing") top.append(node("span", `${repo.branch} failing`, "work-badge bad"));
    li.append(top);
    const lines = node("ul", undefined, "repo-lines");
    const line = (text, href) => {
      const item = node("li");
      item.append(href ? link(text, href, "repo-link") : node("span", text));
      lines.append(item);
    };
    if (repo.commits.length) {
      const who = repo.pushers.slice(0, 2).join(", ") || "Someone";
      line(`${who} pushed ${plural(repo.commits.length, "commit")} to ${repo.branch} — “${repo.commits[0].message}”`, `${repo.url}/commits/${repo.branch}`);
    }
    for (const release of repo.releases) line(`${release.tag} released${release.by ? ` by ${release.by}` : ""}`, release.url);
    if (repo.merged.length) line(`${plural(repo.merged.length, "pull request")} merged: ${repo.merged.slice(0, 2).map((pr) => pr.title).join(" · ")}`, repo.merged[0].url);
    li.append(lines);

    const actions = node("div", undefined, "repo-actions");
    const local = repo.local || {};
    if (local.ok && local.behind) {
      actions.append(node("span", `Your copy is ${local.behind} behind`, "work-meta"));
      if (local.can_pull && !ui().phone()) {
        const pull = node("button", "Pull", "secondary small");
        pull.type = "button";
        pull.addEventListener("click", async () => {
          pull.disabled = true;
          pull.textContent = "Pulling…";
          try {
            const result = await ui().request("/api/v1/repos/pull", "POST", {repo: repo.repo});
            ui().notify(result.message);
          } catch (error) {
            ui().notify(error.message, true);
          }
          repos(true);
        });
        actions.append(pull);
      } else if (!local.can_pull) {
        const why = local.changes ? "it has uncommitted changes" : !local.on_main ? `it's on ${local.branch}` : local.ahead ? "it has your own commits too" : "";
        if (why) actions.append(node("span", `· pull it yourself: ${why}`, "work-meta"));
      }
    }
    const seen = node("button", "Seen", "link small");
    seen.type = "button";
    seen.addEventListener("click", async () => {
      try { await ui().request("/api/v1/repos/seen", "POST", {repo: repo.repo}); } catch { /* keep it */ }
      repos(true);
    });
    actions.append(seen);
    li.append(actions);
    return li;
  }

  $("work-refresh").addEventListener("click", () => { refresh(true); repos(true); });
  window.BridgeWork = {refresh: () => { refresh(false); repos(false); }};
})();
