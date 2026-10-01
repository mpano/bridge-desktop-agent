"use strict";

// Today: your day (meetings + Bridge's plan), what's waiting for your OK, the inbox at a
// glance and who you're waiting on. Everything is built with textContent, never HTML.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let data = null;
  let approvals = [];
  let loading = false;

  const KIND = {focus: "Focus block", email: "Email", task: "Task", break: "Break", admin: "Admin", meeting: "Meeting"};

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function button(label, className, onClick) {
    const element = node("button", label, className);
    element.type = "button";
    element.addEventListener("click", onClick);
    return element;
  }

  const time = (iso) => new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
  const dayKey = () => new Date().toISOString().slice(0, 10);

  // Ticked plan blocks and "already added to calendar" live in this browser only.
  function stored(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  }
  function store(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      const [today, pending] = await Promise.all([
        ui().request("/api/v1/today"),
        ui().request("/api/v1/approvals"),
      ]);
      data = today;
      approvals = pending.approvals;
      render();
    } catch (error) {
      ui().notify(error.message, true);
    } finally {
      loading = false;
    }
  }

  // Header ---------------------------------------------------------------------------------

  function greeting() {
    const hour = new Date().getHours();
    const part = hour >= 5 && hour < 12 ? "Good morning" : hour >= 12 && hour < 18 ? "Good afternoon" : "Good evening";
    const name = (ui().owner().name || "").trim().split(/\s+/)[0];
    return name ? `${part}, ${name}.` : `${part}.`;
  }

  function summary(items) {
    const meetings = items.filter((item) => item.kind === "meeting").length;
    const focus = items.filter((item) => item.kind === "focus").length;
    const parts = [];
    if (meetings) parts.push(`${meetings} meeting${meetings === 1 ? "" : "s"}`);
    if (focus) parts.push(`${focus} focus block${focus === 1 ? "" : "s"}`);
    const inbox = data.inbox && data.inbox.fresh ? data.inbox.counts.urgent + data.inbox.counts.reply : 0;
    if (inbox) parts.push(`${inbox} email${inbox === 1 ? "" : "s"} that need you`);
    let text = parts.length ? `${parts.join(", ").replace(/, ([^,]*)$/, " and $1")}.` : "A clear day so far.";
    if (approvals.length) text += ` ${approvals.length === 1 ? "One thing is" : `${approvals.length} things are`} waiting for your OK.`;
    return text;
  }

  // Your day ---------------------------------------------------------------------------------

  function dayItems() {
    const items = data.events.filter((event) => !event.all_day).map((event) => ({
      kind: "meeting", title: event.title || "Busy", start: event.start, end: event.end,
      meta: event.location || "Meeting",
    }));
    for (const event of data.events.filter((item) => item.all_day)) {
      items.push({kind: "allday", title: event.title, start: null, end: null, meta: "All day"});
    }
    const plan = data.plan ? data.plan.blocks : [];
    plan.forEach((block, index) => items.push({
      kind: block.kind, title: block.title, start: block.start, end: block.end,
      meta: block.why || KIND[block.kind] || "", id: `${data.plan.id}:${index}`,
    }));
    return items.sort((a, b) => (a.start || "").localeCompare(b.start || ""));
  }

  function renderDay(items) {
    const list = $("day-list");
    list.replaceChildren();
    const done = stored(`bridge.done.${dayKey()}`, {});
    const now = Date.now();
    for (const item of items) {
      const row = node("li", undefined, "day-row");
      const live = item.start && new Date(item.start) <= now && now < new Date(item.end);
      if (live) row.classList.add("now");
      const when = item.start ? `${time(item.start)}–${time(item.end)}` : "All day";
      row.append(node("span", when, "mono when"), node("span", undefined, `mark k-${item.kind}`));
      const text = node("div", undefined, "what");
      const title = node("span", item.title, "title");
      text.append(title, node("span", live ? `Now · ${item.meta}` : item.meta, "meta"));
      row.append(text);
      if (item.id) {
        const ticked = Boolean(done[item.id]);
        if (ticked) title.classList.add("done");
        const check = button("", ticked ? "check on" : "check", () => {
          done[item.id] = !ticked;
          store(`bridge.done.${dayKey()}`, done);
          render();
        });
        check.setAttribute("aria-label", `${ticked ? "Mark not done" : "Mark done"}: ${item.title}`);
        check.setAttribute("aria-pressed", String(ticked));
        row.append(check);
      } else {
        row.append(node("span"));
      }
      list.append(row);
    }
    const blocks = items.filter((item) => item.id);
    $("day-count").textContent = blocks.length ? `${blocks.filter((item) => done[item.id]).length} of ${blocks.length} done` : "";
    const empty = $("day-empty");
    empty.hidden = items.length > 0;
    empty.textContent = data.calendar_error || "Nothing on your calendar today. Plan your day and Bridge fits focus time around what matters.";
    const applied = data.plan && stored("bridge.applied", []).includes(data.plan.id);
    $("day-plan").textContent = data.plan ? "Replan" : "Plan my day";
    $("day-apply").hidden = !data.plan || !data.plan.blocks.length || applied;
  }

  // Waiting for your OK ---------------------------------------------------------------------

  function describe(item) {
    const a = item.arguments || {};
    const join = (value) => (Array.isArray(value) ? value.join(", ") : value);
    const preview = (text) => (text ? `“${String(text).replace(/\s+/g, " ").slice(0, 110)}${String(text).length > 110 ? "…" : ""}”` : "");
    switch (item.action) {
      case "email_send": return [`Email ${join(a.to) || "someone"}`, [a.subject, preview(a.body)].filter(Boolean).join(" — ")];
      case "email_create_draft": return [`Draft to ${join(a.to) || "someone"}`, a.subject || ""];
      case "slack_send_message": return [`Message ${a.channel}`, preview(a.text)];
      case "messages_send": return [`Text ${a.recipient || a.to || "someone"}`, preview(a.text || a.message)];
      case "focus_start": return [`Focus for ${a.minutes} min`, a.task || "Calendar · Slack · music"];
      case "plan_day_apply": return ["Add your plan to the calendar", `${(a.blocks || []).length} time blocks`];
      default: return [item.message, ""];
    }
  }

  function renderApprovals() {
    $("approvals-card").hidden = approvals.length === 0;
    const list = $("approvals-list");
    list.replaceChildren();
    for (const item of approvals.slice(0, 3)) {
      const [title, detail] = describe(item);
      const li = node("li", undefined, "approval");
      li.append(node("strong", title));
      if (detail) li.append(node("span", detail, "muted"));
      const actions = node("div", undefined, "row-actions");
      const go = /send|message|text|email/i.test(item.action) ? "Send" : "Approve";
      actions.append(
        button(go, "primary small", () => decide(item.token, true)),
        button("Review", "secondary small", () => ui().view("chat")),
        button("Don't", "ghost small", () => decide(item.token, false)),
      );
      li.append(actions);
      list.append(li);
    }
  }

  async function decide(token, approved) {
    await ui().decideToken(token, approved);
    refresh();
  }

  // Inbox -------------------------------------------------------------------------------------

  function ago(seconds) {
    const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
    return minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
  }

  function renderInbox() {
    const body = $("inbox-body");
    body.replaceChildren();
    $("inbox-refresh").hidden = !data.inbox;
    if (!data.gmail) {
      body.append(node("p", "Connect Gmail and Bridge sorts your inbox by what needs you.", "empty"),
        button("Connect Gmail", "secondary small", () => ui().view("connections")));
      return;
    }
    if (!data.inbox) {
      body.append(node("p", "See what needs you without opening Gmail.", "empty"),
        button("Check my inbox", "secondary small", checkInbox));
      return;
    }
    const counts = data.inbox.counts;
    const tiles = node("div", undefined, "tiles");
    for (const [key, label, tone] of [["urgent", "urgent", "warm"], ["reply", "need a reply", ""], ["fyi", "FYI", ""]]) {
      const tile = node("div", undefined, `tile ${tone}`);
      tile.append(node("span", String(counts[key] || 0), "count"), node("span", label));
      tiles.append(tile);
    }
    body.append(tiles);
    const top = node("ul", undefined, "plain mail-top");
    for (const item of data.inbox.top.slice(0, 3)) {
      const li = node("li");
      li.append(node("strong", item.from), node("span", item.summary, "muted"));
      top.append(li);
    }
    if (data.inbox.top.length) body.append(top);
    body.append(node("p", `Checked ${ago(data.inbox.at)}`, "faint small-text"));
  }

  async function checkInbox() {
    const body = $("inbox-body");
    body.replaceChildren(node("p", "Reading your unread email…", "empty"));
    try {
      const result = await ui().request("/api/v1/inbox/triage", "POST");
      data.inbox = result.summary;
      render();
    } catch (error) {
      ui().notify(error.message, true);
      renderInbox();
    }
  }

  // Follow-ups ---------------------------------------------------------------------------------

  function renderWaiting() {
    $("waiting-card").hidden = data.followups.length === 0;
    const list = $("waiting-list");
    list.replaceChildren();
    for (const item of data.followups.slice(0, 4)) {
      const li = node("li", undefined, "waiting");
      const who = node("span");
      who.append(node("strong", item.name), node("span", ` — ${item.about}`, "muted"));
      const due = new Date(item.due);
      const label = item.status === "overdue" ? "overdue" : `by ${due.toLocaleDateString([], {weekday: "short"})}`;
      li.append(who, node("span", label, item.status === "overdue" ? "pill warm" : "mono faint"));
      list.append(li);
    }
  }

  // Focus -------------------------------------------------------------------------------------

  function renderFocus() {
    const focus = data.focus || {active: false};
    $("focus-banner").hidden = !focus.active;
    $("today-focus").textContent = focus.active ? "Stop focus" : "Start focus";
    if (focus.active) {
      const about = focus.task ? ` on ${focus.task}` : "";
      $("focus-text").textContent = `Focusing${about} until ${focus.until} · ${focus.minutes_left} min left`;
    }
  }

  async function stopFocus() {
    try {
      await ui().request("/api/v1/focus/stop", "POST");
    } catch (error) {
      ui().notify(error.message, true);
    }
    refresh();
  }

  // Render ------------------------------------------------------------------------------------

  function render() {
    if (!data) return;
    const items = dayItems();
    $("today-date").textContent = new Date().toLocaleDateString([], {weekday: "long", day: "numeric", month: "long"});
    $("today-greeting").textContent = greeting();
    $("today-summary").textContent = summary(items);
    renderDay(items);
    renderApprovals();
    renderInbox();
    renderWaiting();
    renderFocus();
  }

  // Events ------------------------------------------------------------------------------------

  $("today-ask").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("today-input").value.trim();
    if (!text || ui().busy()) return;
    $("today-input").value = "";
    ui().ask(text);
  });
  document.querySelectorAll("#today-chips [data-ask]").forEach((chip) => {
    chip.addEventListener("click", () => { if (!ui().busy()) ui().ask(chip.dataset.ask); });
  });

  $("day-plan").addEventListener("click", () => ui().run(async () => {
    $("day-plan").textContent = "Planning…";
    try {
      await ui().request("/api/v1/today/plan", "POST", {day: "today"});
    } finally {
      await refresh();
    }
  }));

  $("day-apply").addEventListener("click", () => ui().run(async () => {
    const plan = data.plan;
    const result = await ui().request("/api/v1/today/plan/apply", "POST", {plan_id: plan.id});
    store("bridge.applied", [...stored("bridge.applied", []), plan.id].slice(-20));
    ui().notify(`Added ${result.count} block${result.count === 1 ? "" : "s"} to your calendar.`);
    render();
  }));

  $("inbox-refresh").addEventListener("click", checkInbox);

  const dialog = $("focus-dialog");
  $("today-focus").addEventListener("click", () => {
    if (data && data.focus && data.focus.active) stopFocus();
    else dialog.showModal();
  });
  $("focus-stop").addEventListener("click", stopFocus);
  $("focus-cancel").addEventListener("click", () => dialog.close());
  $("focus-form").addEventListener("submit", (event) => {
    event.preventDefault();
    dialog.close();
    const minutes = Number(new FormData(event.target).get("focus-minutes"));
    const task = $("focus-task").value.trim();
    ui().run(async () => {
      const result = await ui().request("/api/v1/focus/start", "POST", {minutes, task});
      const skipped = result.skipped.length ? ` Skipped: ${result.skipped.join("; ")}` : "";
      ui().notify(`Focusing until ${result.until}.${skipped}`);
      await refresh();
    });
  });

  // Keep "now" and the focus countdown current while the window is open.
  setInterval(() => { if (document.visibilityState === "visible" && !$("view-today").hidden) refresh(); }, 60000);

  window.BridgeToday = {refresh};
})();
