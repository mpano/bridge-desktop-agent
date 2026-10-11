"use strict";

// The weekly report on Today: from Friday afternoon to Monday noon (or whenever you open it
// from Automations). Bridge drafts it from your week; you edit it right here, then copy it or
// send it to a Slack channel or by email. Send asks once more, naming where it goes.
(() => {
  const {button, node, link} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let report = null;
  let opened = false;
  let writing = false;
  let saveTimer = null;
  let savedLabel = null;  // "Saving…" / "Saved" under the draft.

  const hiddenKey = () => `bridge-report-hidden-${report.week}`;
  function hidden() {
    try { return localStorage.getItem(hiddenKey()) === "1"; } catch { return false; }
  }

  async function refresh() {
    try {
      report = await ui().request("/api/v1/report");
      render();
    } catch {
      $("report-card").hidden = true;
    }
  }

  async function write(fresh) {
    if (writing) return;
    writing = true;
    render();
    try {
      report = await ui().request("/api/v1/report/write", "POST", {fresh});
    } catch (error) {
      ui().notify(error.message, true);
    } finally {
      writing = false;
      render();
    }
  }

  function saveSoon(text) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        report = await ui().request("/api/v1/report/edit", "POST", {week: report.week, text});
        if (savedLabel) savedLabel.textContent = "Saved";
      } catch (error) {
        ui().notify(error.message, true);
      }
    }, 700);
  }

  // "#team", "@sam" or "Sam Lee" go to Slack; addresses with an @ and a dot go by email.
  function destination(value) {
    const text = value.trim();
    const addresses = text.split(/[,;\s]+/).filter(Boolean);
    if (addresses.length && addresses.every((a) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(a))) {
      return {email: addresses, label: addresses.join(", ")};
    }
    return text ? {slack: text, label: text.startsWith("#") || text.startsWith("@") ? text : `Slack: ${text}`} : null;
  }

  async function send(where, text, area) {
    try {
      report = await ui().request("/api/v1/report/send", "POST", {week: report.week, text, ...(where.email ? {email: where.email} : {slack: where.slack})});
      ui().notify(`Sent to ${report.sent[report.sent.length - 1].to}.`);
    } catch (error) {
      ui().notify(error.message, true);
    }
    render();
    if (area) area.focus();
  }

  function grow(area) {
    area.style.height = "auto";
    area.style.height = `${Math.min(area.scrollHeight + 2, 520)}px`;
  }

  function render() {
    const card = $("report-card");
    if (!report || (!opened && (!report.due || hidden()))) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    $("report-label").textContent = `Weekly report · ${report.range}`;
    $("report-rewrite").hidden = !report.text || writing;
    const body = $("report-body");
    body.replaceChildren();

    if (!report.text) {
      const empty = node("div", undefined, "report-empty");
      empty.append(node("p", writing ? "Gathering your week from GitHub, Jira, promises and your calendar…"
        : "Bridge drafts it from what you merged, reviewed and closed, the promises you kept and what's coming up. You edit it before anything is sent.", "hint"));
      if (!writing) empty.append(button("Write it now", "primary small", () => write(false)));
      body.append(empty);
      return;
    }

    if (report.stats.length) {
      const stats = node("div", undefined, "report-stats");
      for (const item of report.stats) {
        const stat = node("span", undefined, "report-stat");
        stat.append(node("strong", String(item.count)), document.createTextNode(` ${item.label}`));
        stats.append(stat);
      }
      body.append(stats);
    }

    const area = node("textarea", undefined, "report-text");
    area.value = report.text;
    area.setAttribute("aria-label", "Your weekly report");
    area.spellcheck = true;
    area.disabled = writing;
    area.addEventListener("input", () => {
      grow(area);
      if (savedLabel) savedLabel.textContent = "Saving…";
      saveSoon(area.value);
    });
    body.append(area);
    requestAnimationFrame(() => grow(area));

    if (report.links.length) {
      const links = node("div", undefined, "report-links");
      for (const item of report.links.slice(0, 10)) links.append(link(item.ref, item.url, "report-link"));
      body.append(links);
    }

    const foot = node("div", undefined, "report-foot");
    const status = node("span", report.edited ? "Your edits are saved" : "Draft — edit anything", "faint report-saved");
    savedLabel = status;
    const copy = button("Copy", "secondary small", async () => {
      try {
        await navigator.clipboard.writeText(area.value);
        ui().notify("Copied.");
      } catch {
        area.select();
        document.execCommand("copy");
      }
    });
    const to = node("input", undefined, "report-to");
    to.value = report.destination || "";
    to.placeholder = report.can_slack ? "#team-updates or name@company.com" : "name@company.com";
    to.setAttribute("aria-label", "Send to");
    to.maxLength = 300;
    const go = node("div", undefined, "report-send");
    const sendButton = button("Send", "primary small", () => {
      const where = destination(to.value);
      if (!where) {
        to.focus();
        return;
      }
      if (where.slack && !report.can_slack) return ui().notify("Connect Slack first.", true);
      if (where.email && !report.can_email) return ui().notify("Connect Gmail first.", true);
      // Ask once more, naming exactly where it goes.
      go.replaceChildren(node("span", `Send this to ${where.label}?`, "report-confirm"),
        button("Send now", "primary small", () => send(where, area.value)),
        button("Cancel", "ghost small", render));
    });
    to.addEventListener("keydown", (event) => { if (event.key === "Enter") sendButton.click(); });
    go.append(to, sendButton);
    foot.append(status, copy, go);
    body.append(foot);

    if (report.sent.length) {
      const last = report.sent[report.sent.length - 1];
      const when = new Date(last.at * 1000).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
      body.append(node("p", `✓ Sent to ${last.to} · ${when}`, "report-sent"));
    }
  }

  $("report-rewrite").addEventListener("click", () => {
    if (report.edited && !confirm("Rewrite it from the latest data? Your edits will be replaced.")) return;
    write(true);
  });
  $("report-hide").addEventListener("click", () => {
    opened = false;
    try { localStorage.setItem(hiddenKey(), "1"); } catch { /* fine */ }
    render();
  });

  window.BridgeReport = {
    refresh,
    // From Automations: show it on Today now, writing it if there's none yet.
    async open() {
      opened = true;
      ui().view("today");
      await refresh();
      if (report && !report.text) write(false);
      $("report-card").scrollIntoView({behavior: "smooth", block: "start"});
    },
  };
})();
