"use strict";

// The ⌥Space command bar: type a request or pick an action. It knows which window you were
// in, so "Reply to this" and "Summarize this" come first. ↵ runs it here; ⌘↵ runs it and
// opens the Bridge window.
(() => {
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  const {node, button} = mini;
  let seen = null;
  let pick = 0;
  let rows = [];
  let busy = false;

  const BASE = [
    {kind: "day", glyph: "D", title: "Plan my day", sub: "Time blocks around your meetings"},
    {kind: "mail", glyph: "M", title: "What needs my attention?", sub: "Sort unread email by what it needs"},
    {kind: "focus", glyph: "F", title: "Focus for 90 minutes", sub: "Calendar · Slack · music"},
    {kind: "day", glyph: "B", title: "Brief me", sub: "Today at a glance"},
  ];

  function suggestions() {
    const screen = seen && !seen.private ? [
      {kind: "screen", glyph: "↩", title: "Reply to this", sub: `${seen.app}${seen.window ? ` — ${seen.window}` : ""}`},
      {kind: "screen", glyph: "≡", title: "Summarize this", sub: "The window you're looking at"},
      {kind: "screen", glyph: "文", title: "Translate this to English", sub: "The window you're looking at"},
    ] : [];
    const query = $("c-input").value.trim();
    const all = [...screen, ...BASE];
    let list = query ? all.filter((item) => item.title.toLowerCase().includes(query.toLowerCase())) : all;
    if (query) list = [{kind: "ask", glyph: "?", title: `Ask Bridge: “${query}”`, sub: "Bridge works out the steps", text: query}, ...list];
    return list.slice(0, 7);
  }

  function size() {
    requestAnimationFrame(() => mini.native({action: "size", height: Math.ceil($("c-card").getBoundingClientRect().height) + 2}));
  }

  function renderList() {
    rows = suggestions();
    pick = Math.min(pick, rows.length - 1);
    const list = $("c-list");
    list.replaceChildren();
    list.hidden = busy || !rows.length;
    rows.forEach((row, index) => {
      const item = node("li");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(index === pick));
      const choice = button("", `cmd-opt${index === pick ? " on" : ""}`, () => run(row, false));
      choice.addEventListener("mouseenter", () => { pick = index; renderList(); });
      const words = node("span", undefined, "cmd-words");
      words.append(node("span", row.title, "cmd-title"), node("span", row.sub, "faint"));
      choice.append(node("span", row.glyph, `cmd-glyph ${row.kind}`), words);
      item.append(choice);
      list.append(item);
    });
    size();
  }

  function reply(text, {open = false} = {}) {
    const box = $("c-reply");
    box.hidden = !text;
    box.replaceChildren();
    if (text) box.append(node("p", text));
    if (open) box.append(button("Open in Bridge", "link", () => mini.native({action: "open", view: "chat"})));
    size();
  }

  function approvalCard(item) {
    const info = mini.describe(item);
    const card = node("section", undefined, "mini-now warm");
    card.append(node("p", "Waiting for your OK", "label attention"), node("strong", info.title));
    if (info.detail) card.append(node("p", info.detail, "muted small-print"));
    const actions = node("div", undefined, "row-actions");
    actions.append(
      button(info.go, "primary small", () => decide(item.token, true)),
      button("Don't", "ghost small", () => decide(item.token, false)),
    );
    card.append(actions);
    const box = $("c-reply");
    box.hidden = false;
    box.replaceChildren(card);
    size();
  }

  async function finish(result, open) {
    if (result.status === "confirmation_required") {
      const {approvals} = await mini.request("/api/v1/approvals");
      const mine = approvals.find((item) => item.request_id === result.request_id) || approvals[0];
      if (mine && !open) return approvalCard(mine);
    }
    reply(result.message, {open: true});
  }

  async function run(row, open) {
    if (busy) return;
    const text = row.text || row.title;
    busy = true;
    $("c-input").value = text;
    renderList();
    reply("Working on it…");
    if (open) mini.native({action: "open", view: "chat"});
    try {
      await finish(await mini.run(text), open);
    } catch (error) {
      reply(error.message);
    } finally {
      busy = false;
      $("c-list").hidden = true;
    }
  }

  async function decide(token, approved) {
    reply(approved ? "Sending your OK…" : "Okay, not doing it…");
    try {
      reply((await mini.decide(token, approved)).message, {open: true});
    } catch (error) {
      reply(error.message);
    }
  }

  async function peek() {
    try {
      seen = (await mini.request("/api/v1/screen/peek")).window;
    } catch {
      seen = null;
    }
    $("c-chip").hidden = !seen;
    if (seen) $("c-chip").textContent = seen.private ? `${seen.app} · private` : seen.app;
    renderList();
  }

  $("c-input").addEventListener("input", () => { pick = 0; reply(""); renderList(); });
  $("c-input").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      pick = (pick + (event.key === "ArrowDown" ? 1 : rows.length - 1)) % Math.max(rows.length, 1);
      renderList();
    } else if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      const row = rows[pick] || (event.target.value.trim() ? {text: event.target.value.trim()} : null);
      if (row) run(row, event.metaKey || event.ctrlKey);
    }
  });

  mini.onShown(() => {
    busy = false;
    pick = 0;
    $("c-input").value = "";
    reply("");
    $("c-input").focus();
    peek();
  });
  mini.ready.then(peek);
})();
