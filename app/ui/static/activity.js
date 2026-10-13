"use strict";

// Activity: everything Bridge sent or changed for you, newest first, grouped by day, with
// Undo where the service allows it (and why not, where it doesn't).
(() => {
  const {button, node, link, empty} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;

  const FAMILY = {
    slack_send_message: ["Slack", "violet"], email_send: ["Email", "warm"], email_create_draft: ["Email", "warm"],
    messages_send: ["Messages", "green"], jira_comment: ["Jira", "cool"], jira_move: ["Jira", "cool"],
    jira_create: ["Jira", "cool"], github_comment: ["GitHub", ""], github_create_issue: ["GitHub", ""],
    calendar_create_event: ["Calendar", "cool"], mac_calendar_create_event: ["Calendar", "cool"],
    calendar_delete_event: ["Calendar", "cool"], reminders_add: ["Reminders", "warm"], notes_create: ["Notes", "warm"],
    focus_start: ["Focus", "violet"], recipe_create: ["Recipe", "violet"], watch_create: ["Watcher", "violet"],
    operate_mac: ["Mac", ""],
  };

  function day(ts) {
    const date = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date(Date.now() - 86400000);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
    return date.toLocaleDateString([], {weekday: "long", day: "numeric", month: "short"});
  }

  async function undo(item, control) {
    control.disabled = true;
    control.textContent = "Undoing…";
    try {
      await ui().request("/api/v1/actions/undo", "POST", {id: item.id});
      ui().notify("Undone.");
      refresh();
    } catch (error) {
      ui().notify(error.message, true);
      control.disabled = false;
      control.textContent = "Undo";
    }
  }

  function row(item) {
    const li = node("li", undefined, `act-row ${item.status}`);
    const [source, tone] = FAMILY[item.tool] || ["Bridge", ""];
    li.append(node("span", source, `src ${tone}`));
    const words = node("span", undefined, "line-main");
    const title = item.url ? link(item.summary, item.url, "act-title") : node("span", item.summary, "act-title");
    words.append(title, node("span", new Date(item.at * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), "mono faint"));
    li.append(words);
    if (item.status === "undone") li.append(node("span", "Undone ✓", "pill good"));
    else if (item.can_undo) {
      const control = button("Undo", "secondary small", () => undo(item, control));
      li.append(control);
    } else if (item.why_not) li.append(node("span", item.why_not, "act-why faint"));
    return li;
  }

  async function refresh() {
    const root = $("activity-root");
    if (!root) return;
    let items = [];
    try { items = (await ui().request("/api/v1/actions")).actions; } catch (error) { ui().notify(error.message, true); }
    root.replaceChildren();
    if (!items.length) {
      const card = node("section", undefined, "card");
      card.append(empty("bolt", "Nothing yet", "When Bridge sends a message, moves a ticket or adds an event for you, it shows up here — with Undo where possible."));
      root.append(card);
      return;
    }
    let current = null;
    let list = null;
    for (const item of items) {
      const label = day(item.at);
      if (label !== current) {
        current = label;
        const card = node("section", undefined, "card list-card act-day");
        card.append(node("h2", label, "label act-day-title"));
        list = node("ul", undefined, "lines");
        card.append(list);
        root.append(card);
      }
      list.append(row(item));
    }
  }

  window.BridgeActivity = {refresh};
})();
