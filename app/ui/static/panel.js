"use strict";

// The compact menu bar panel: ask or speak, the one thing that matters right now (an
// approval, your focus timer, or your next meeting), four quick actions, and the reply.
(() => {
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  const {node, button} = mini;
  let today = null;
  let approvals = [];
  let desk = null;  // What needs you, recording: the same view as the desk avatar.
  let working = false;

  async function refresh() {
    try {
      [today, approvals, desk] = await Promise.all([
        mini.request("/api/v1/today"),
        mini.request("/api/v1/approvals").then((data) => data.approvals),
        mini.request("/api/v1/buddy").catch(() => null),
      ]);
    } catch {
      $("p-status").textContent = "Open Bridge to finish setting up";
      return;
    }
    render();
  }

  function minutesUntil(iso) {
    return Math.round((new Date(iso) - Date.now()) / 60000);
  }

  function nextMeeting() {
    return (today.events || [])
      .filter((event) => !event.all_day && new Date(event.end) > Date.now())
      .sort((a, b) => a.start.localeCompare(b.start))[0];
  }

  function nowCard() {
    const box = $("p-now");
    box.replaceChildren();
    if (approvals.length) {
      const item = approvals[0];
      const info = mini.describe(item);
      const card = node("section", undefined, "mini-now warm");
      card.append(node("p", "Waiting for your OK", "label attention"), node("strong", info.title));
      if (info.detail) card.append(node("p", info.detail, "muted small-print"));
      const row = node("div", undefined, "row-actions");
      row.append(
        button(info.go, "primary small", () => decide(item.token, true)),
        button("Review", "secondary small", () => mini.native({action: "open", view: "chat"})),
        button("Don't", "ghost small", () => decide(item.token, false)),
      );
      card.append(row);
      box.append(card);
      return;
    }
    const focus = today.focus || {};
    if (focus.active) {
      const card = node("section", undefined, "mini-now focus");
      const top = node("div", undefined, "focus-top");
      top.append(node("span", `${focus.minutes_left} min`, "focus-time"), button("Stop", "on-accent small", stopFocus));
      card.append(node("p", focus.task ? `Focusing on ${focus.task}` : "Focusing", "label"), top,
        node("p", `Until ${focus.until} · Slack paused`, "small-print"));
      box.append(card);
      return;
    }
    const meeting = nextMeeting();
    if (meeting) {
      const card = node("section", undefined, "mini-now");
      const minutes = minutesUntil(meeting.start);
      const when = minutes <= 0 ? "now" : minutes < 60 ? `in ${minutes} min` : `at ${new Date(meeting.start).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false})}`;
      const line = node("p", undefined, "mini-line");
      line.append(node("strong", meeting.title || "Busy"), node("span", ` ${when}`, "muted"));
      card.append(node("p", "Next", "label"), line);
      if (meeting.location) card.append(node("p", meeting.location, "muted small-print"));
      box.append(card);
      return;
    }
    box.append(node("p", "Nothing waiting for you. Ask anything, or try one of these.", "mini-quiet"));
  }

  // At a glance: recording, what needs you, the rest of today.
  function glance() {
    const box = $("p-glance");
    box.replaceChildren();
    const time = (iso) => new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
    if (desk && desk.recording) {
      const row = node("div", undefined, "glance-rec");
      row.append(node("span", undefined, "mt-rec"), node("span", `Taking notes · ${desk.recording}`, "grow"),
        button("Stop", "ghost small", async () => {
          try { await mini.request("/api/v1/meetings/stop", "POST", {}); } catch { /* shown next time */ }
          refresh();
        }));
      box.append(row);
    }
    const needs = (desk && desk.needs) || [];
    if (needs.length) {
      const list = node("div", undefined, "glance-list");
      list.append(node("p", "Needs you", "label"));
      for (const item of needs) {
        const row = button("", "glance-row", () => mini.native({action: "open", view: item.view || "today"}));
        row.append(node("span", String(item.count), "glance-count"), node("span", item.label, "grow"), node("span", "›", "faint"));
        list.append(row);
      }
      box.append(list);
    }
    const next = nextMeeting();
    const later = (today.events || [])
      .filter((event) => !event.all_day && new Date(event.start) > Date.now() && (!next || event.start !== next.start))
      .sort((a, b) => a.start.localeCompare(b.start)).slice(0, 3);
    if (later.length) {
      const list = node("div", undefined, "glance-list");
      list.append(node("p", "Later today", "label"));
      for (const event of later) {
        const row = node("div", undefined, "glance-event");
        row.append(node("span", time(event.start), "mono faint"), node("span", event.title || "Busy", "grow"));
        list.append(row);
      }
      box.append(list);
    }
    box.hidden = !box.childElementCount || !$("p-reply").hidden;
  }

  function render() {
    const name = (today.focus || {}).active ? "Focusing" : approvals.length ? `${approvals.length === 1 ? "1 thing needs" : `${approvals.length} things need`} you` : "Ready";
    if (!working) $("p-status").textContent = name;
    $("p-focus").textContent = (today.focus || {}).active ? "Stop focus" : "Focus 90 min";
    nowCard();
    glance();
  }

  // Asking -----------------------------------------------------------------------------------

  function reply(text, {working: busy = false, open = false} = {}) {
    const box = $("p-reply");
    box.hidden = !text;
    $("p-glance").hidden = Boolean(text) || !$("p-glance").childElementCount;
    box.replaceChildren();
    if (!text) return;
    box.append(node("p", text, busy ? "muted" : ""));
    if (open) box.append(button("Open in Bridge", "link", () => mini.native({action: "open", view: "chat"})));
  }

  async function ask(text) {
    if (!text || working) return;
    working = true;
    $("p-status").textContent = "Working…";
    reply("Working on it…", {working: true});
    try {
      const result = await mini.run(text);
      reply(result.status === "confirmation_required" ? "This needs your OK — see above." : result.message, {open: true});
    } catch (error) {
      reply(error.message);
    } finally {
      working = false;
      refresh();
    }
  }

  async function decide(token, approved) {
    reply(approved ? "Sending your OK…" : "Okay, not doing it…", {working: true});
    try {
      const result = await mini.decide(token, approved);
      reply(result.message, {open: true});
    } catch (error) {
      reply(error.message);
    }
    refresh();
  }

  async function stopFocus() {
    try { await mini.request("/api/v1/focus/stop", "POST"); } catch (error) { reply(error.message); }
    refresh();
  }

  // Voice, passed along by the app ----------------------------------------------------------

  const LISTENING = {starting: "Getting ready…", recording: "Listening…", transcribing: "Finding your words…", processing: "On it…"};

  function voice(status) {
    const busy = status.state in LISTENING;
    $("p-listen").hidden = !busy;
    $("p-ask").hidden = busy;
    if (busy) $("p-listen-text").textContent = status.transcript ? `“${status.transcript}”` : LISTENING[status.state];
    if (status.state === "speaking" || (status.state === "ready" && status.message)) reply(status.message, {open: true});
    if (status.state === "approval" || status.state === "ready") refresh();
  }

  // Events -----------------------------------------------------------------------------------

  $("p-ask").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("p-input").value.trim();
    $("p-input").value = "";
    ask(text);
  });
  $("p-mic").addEventListener("click", () => mini.native({action: "speak"}));
  $("p-stop").addEventListener("click", () => mini.native({action: "stop"}));
  $("p-open").addEventListener("click", () => mini.native({action: "open", view: "today"}));
  document.querySelectorAll("[data-ask]").forEach((chip) => chip.addEventListener("click", () => ask(chip.dataset.ask)));
  $("p-focus").addEventListener("click", async () => {
    if ((today && today.focus || {}).active) return stopFocus();
    try {
      const result = await mini.request("/api/v1/focus/start", "POST", {minutes: 90, task: ""});
      reply(`Focusing until ${result.until}.${result.skipped.length ? ` Skipped: ${result.skipped.join("; ")}` : ""}`);
    } catch (error) {
      reply(error.message);
    }
    refresh();
  });

  mini.onShown(() => { refresh(); $("p-input").focus(); });
  window.BridgePanel = {voice};
  mini.ready.then(refresh);
  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 30000);
})();
