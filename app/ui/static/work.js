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

  $("work-refresh").addEventListener("click", () => refresh(true));
  window.BridgeWork = {refresh: () => refresh(false)};
})();
