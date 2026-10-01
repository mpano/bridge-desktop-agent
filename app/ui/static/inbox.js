"use strict";

// Inbox: email sorted by what it needs from you, Bridge's summary of each, and a reply in
// your voice that you send (or skip) yourself. Email text is shown with textContent only.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  const GROUPS = [
    ["urgent", "Urgent"], ["reply", "Needs a reply"], ["fyi", "FYI"], ["newsletter", "Newsletters"],
  ];
  const DRAFT_GROUPS = new Set(["urgent", "reply"]);
  let state = {gmail: true, sorted: null};
  let tab = "reply";
  let selected = null;
  let sorting = false;
  const opened = {};   // message_id → {body, date, earlier, to, subject, can_reply} or {error}
  const drafts = {};   // message_id → text (what's in the box, edits included)
  const drafting = new Set();

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

  // Sent and skipped emails, remembered in this browser for the day.
  const doneKey = () => `bridge.inbox.${new Date().toISOString().slice(0, 10)}`;
  function done() {
    try { return JSON.parse(localStorage.getItem(doneKey())) || {}; } catch { return {}; }
  }
  function mark(id, status) {
    const all = done();
    all[id] = status;
    try { localStorage.setItem(doneKey(), JSON.stringify(all)); } catch { /* private mode */ }
  }

  const name = (from) => (from || "").split("<")[0].trim().replace(/^"|"$/g, "") || from;
  const items = (group) => (state.sorted ? state.sorted.groups[group] || [] : []);
  const open = (group) => items(group).filter((item) => !done()[item.message_id]);

  function shortDate(text) {
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date().toDateString() === date.toDateString();
    return today ? date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false})
      : date.toLocaleDateString([], {weekday: "short"});
  }

  function ago(seconds) {
    const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
    return minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
  }

  async function refresh() {
    try {
      state = await ui().request("/api/v1/inbox");
      if (state.sorted && !open(tab).length) {
        const first = GROUPS.find(([key]) => open(key).length);
        if (first) tab = first[0];
      }
      if (!selected || !items(tab).some((item) => item.message_id === selected)) {
        selected = (open(tab)[0] || items(tab)[0] || {}).message_id || null;
      }
      render();
    } catch (error) {
      ui().notify(error.message, true);
    }
  }

  async function sortInbox() {
    if (sorting) return;
    sorting = true;
    render();
    try {
      await ui().request("/api/v1/inbox/triage", "POST");
      selected = null;
      await refresh();
    } catch (error) {
      ui().notify(error.message, true);
    } finally {
      sorting = false;
      render();
    }
  }

  // Header, tabs, list ----------------------------------------------------------------------

  function renderBadge() {
    const count = open("urgent").length + open("reply").length;
    $("inbox-badge").hidden = !count;
    $("inbox-badge").textContent = String(count);
  }

  function renderTabs() {
    const bar = $("inbox-tabs");
    bar.replaceChildren();
    bar.hidden = !state.sorted;
    for (const [key, label] of GROUPS) {
      const chosen = key === tab;
      const tabButton = button("", `tab${chosen ? " on" : ""}${key === "urgent" ? " urgent" : ""}`, () => {
        tab = key;
        selected = (open(key)[0] || items(key)[0] || {}).message_id || null;
        render();
      });
      tabButton.setAttribute("role", "tab");
      tabButton.setAttribute("aria-selected", String(chosen));
      tabButton.append(node("span", label), node("span", String(open(key).length), "count"));
      bar.append(tabButton);
    }
  }

  function renderList() {
    const list = $("inbox-list");
    list.replaceChildren();
    const status = done();
    for (const item of items(tab)) {
      const chosen = item.message_id === selected;
      const row = button("", `mail${chosen ? " on" : ""}${status[item.message_id] ? " done" : ""}`, () => {
        selected = item.message_id;
        render();
      });
      row.setAttribute("aria-current", chosen ? "true" : "false");
      const top = node("span", undefined, "mail-top");
      top.append(node("strong", name(item.from)));
      if (status[item.message_id]) top.append(node("span", status[item.message_id], `badge ${status[item.message_id]}`));
      top.append(node("span", shortDate(item.date), "mono faint"));
      row.append(top, node("span", item.subject || "(no subject)", "mail-subject"), node("span", item.summary, "mail-summary"));
      list.append(row);
    }
    if (!items(tab).length) list.append(node("p", "Nothing here right now.", "empty"));
  }

  // Detail ------------------------------------------------------------------------------------

  async function load(item, group) {
    if (!opened[item.message_id]) {
      try {
        opened[item.message_id] = await ui().request("/api/v1/inbox/open", "POST", {message_id: item.message_id});
      } catch (error) {
        opened[item.message_id] = {error: error.message};
      }
      if (selected === item.message_id) renderDetail();
    }
    const message = opened[item.message_id];
    if (DRAFT_GROUPS.has(group) && message.can_reply && drafts[item.message_id] === undefined) draft(item);
  }

  async function draft(item) {
    if (drafting.has(item.message_id)) return;
    drafting.add(item.message_id);
    if (selected === item.message_id) renderDetail();
    try {
      const result = await ui().request("/api/v1/inbox/draft", "POST", {message_id: item.message_id, name: ui().owner().name || ""});
      drafts[item.message_id] = result.body;
    } catch (error) {
      ui().notify(error.message, true);
    } finally {
      drafting.delete(item.message_id);
      if (selected === item.message_id) renderDetail();
    }
  }

  function next() {
    const rest = open(tab).filter((item) => item.message_id !== selected);
    selected = (rest[0] || {}).message_id || selected;
  }

  async function send(item, box, remind) {
    const body = box.value.trim();
    if (!body) return;
    box.disabled = true;
    try {
      const result = await ui().request("/api/v1/inbox/send", "POST", {message_id: item.message_id, body, follow_up: remind});
      mark(item.message_id, "sent");
      delete drafts[item.message_id];
      const follow = result.followup ? ` Bridge will tell you if ${name(item.from)} hasn't replied in 3 working days.` : "";
      ui().notify(`Sent to ${name(item.from)}.${follow}`);
      next();
      render();
    } catch (error) {
      box.disabled = false;
      ui().notify(error.message, true);
    }
  }

  function draftCard(item, message, group) {
    const card = node("section", undefined, "draft-card");
    const head = node("div", undefined, "draft-head");
    head.append(node("h3", `Reply to ${name(item.from)}`), node("span", message.to, "mono faint"));
    card.append(head);
    const text = drafts[item.message_id];
    if (text === undefined) {
      if (drafting.has(item.message_id)) {
        card.append(node("p", "Writing a reply in your style…", "drafting"));
      } else {
        const actions = node("div", undefined, "draft-actions");
        actions.append(button("Draft a reply", "secondary small", () => draft(item)));
        card.append(node("p", "Want Bridge to write a reply?", "drafting"), actions);
      }
      return card;
    }
    const label = node("label", "Reply", "sr-only");
    label.htmlFor = "draft-text";
    const box = node("textarea", undefined, "draft-text");
    box.id = "draft-text";
    box.rows = 7;
    box.value = text;
    box.addEventListener("input", () => { drafts[item.message_id] = box.value; });
    const remindLabel = node("label", undefined, "remind");
    const remind = node("input");
    remind.type = "checkbox";
    remind.checked = group === "reply" || group === "urgent";
    remindLabel.append(remind, node("span", "Remind me if no reply in 3 working days"));
    const go = node("button", undefined, "primary");
    go.type = "button";
    go.append(node("span", "Send"), node("kbd", "⌘↵"));
    go.addEventListener("click", () => send(item, box, remind.checked));
    box.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        send(item, box, remind.checked);
      }
    });
    const skip = button("Skip", "secondary", () => { mark(item.message_id, "skipped"); next(); render(); });
    const actions = node("div", undefined, "draft-actions");
    actions.append(go, skip, remindLabel);
    card.append(label, box, actions, node("p", "Sent from your Gmail, in the same thread. Nothing goes out until you press Send.", "faint note"));
    return card;
  }

  function renderDetail() {
    const panel = $("inbox-detail");
    panel.replaceChildren();
    const found = GROUPS.map(([key]) => [key, items(key).find((item) => item.message_id === selected)]).find(([, item]) => item);
    if (!found) return;
    const [group, item] = found;
    const label = GROUPS.find(([key]) => key === group)[1];
    const head = node("header", undefined, "detail-head");
    head.append(node("p", label, "label"), node("h2", item.subject || "(no subject)"));
    const who = node("p", undefined, "who");
    who.append(node("strong", name(item.from)), node("span", ` · ${shortDate(item.date)}`, "faint"));
    head.append(who);
    panel.append(head);

    const note = node("div", undefined, "bridge-note");
    note.append(node("p", item.summary));
    if (item.action) note.append(node("p", `Suggested: ${item.action}`, "suggested"));
    panel.append(note);

    const message = opened[item.message_id];
    if (!message) {
      panel.append(node("p", "Opening the email…", "faint"));
      load(item, group);
      return;
    }
    if (message.error) {
      panel.append(node("p", message.error, "empty"));
      return;
    }
    panel.append(node("blockquote", message.body || "(no text)", "mail-body"));
    if (group !== "newsletter" && message.can_reply) panel.append(draftCard(item, message, group));
    const links = node("div", undefined, "draft-actions");
    const gmail = node("a", "Open in Gmail ↗", "secondary small button-link");
    gmail.href = `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(item.thread_id || "")}`;
    gmail.target = "_blank";
    gmail.rel = "noopener";
    links.append(gmail);
    if (group === "newsletter" || group === "fyi") {
      links.append(button(done()[item.message_id] ? "Done" : "Mark done", "ghost small", () => { mark(item.message_id, "skipped"); next(); render(); }));
    }
    panel.append(links);
    load(item, group);
  }

  // Empty states, render ----------------------------------------------------------------------

  function render() {
    renderBadge();
    const list = $("inbox-list");
    const panel = $("inbox-detail");
    $("inbox-sort").textContent = sorting ? "Sorting…" : state.sorted ? "Refresh" : "Sort my inbox";
    $("inbox-sort").disabled = sorting || !state.gmail;
    $("inbox-when").textContent = state.sorted ? `Sorted by Bridge · ${ago(state.sorted.at)}` : "Inbox";
    renderTabs();
    if (!state.gmail) {
      list.replaceChildren();
      panel.replaceChildren(node("h2", "Connect Gmail"), node("p", "Bridge sorts your unread email by what it needs from you and writes replies in your style. You send them.", "empty"),
        button("Open Connections", "primary", () => ui().view("connections")));
      return;
    }
    if (!state.sorted) {
      list.replaceChildren();
      panel.replaceChildren(node("h2", sorting ? "Reading your unread email…" : "See what needs you"),
        node("p", sorting ? "Bridge is sorting the last two days of unread email." : "Bridge reads your unread email from the last two days and sorts it by what it needs from you.", "empty"));
      if (!sorting) panel.append(button("Sort my inbox", "primary", sortInbox));
      return;
    }
    renderList();
    renderDetail();
  }

  $("inbox-sort").addEventListener("click", sortInbox);
  window.BridgeInbox = {refresh};
})();
