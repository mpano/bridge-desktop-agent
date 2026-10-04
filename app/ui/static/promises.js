"use strict";

// Promises on Today: what you owe people and what you're waiting on, found in your email and
// Slack. Mark one done, change its date, dismiss it, open it where it was said, or write a
// follow-up in your own style and send it in the same thread. Built with textContent only.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let data = null;
  let followups = [];
  let tab = "mine";
  let open = null;  // The promise whose follow-up editor is showing.
  let poll = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function button(label, className, onClick, title) {
    const element = node("button", label, className);
    element.type = "button";
    if (title) {
      element.title = title;
      element.setAttribute("aria-label", title);
    }
    element.addEventListener("click", onClick);
    return element;
  }

  function day(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function dueLabel(due) {
    if (!due) return null;
    const days = Math.round((day(due) - day(data.today)) / 86400000);
    if (days < 0) return ["overdue", "late"];
    if (days === 0) return ["today", "now"];
    if (days === 1) return ["tomorrow", ""];
    if (days < 7) return [day(due).toLocaleDateString([], {weekday: "short"}), ""];
    return [day(due).toLocaleDateString([], {day: "numeric", month: "short"}), ""];
  }

  function ago(seconds) {
    const days = Math.floor((Date.now() / 1000 - seconds) / 86400);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    return `${days} days ago`;
  }

  async function refresh(waiting) {
    if (waiting) followups = waiting;
    try {
      data = await ui().request("/api/v1/commitments");
    } catch (error) {
      $("promises-card").hidden = true;
      return;
    }
    $("promises-card").hidden = false;
    render();
    // While Bridge reads your messages, show its progress.
    clearTimeout(poll);
    if (data.scan.running) poll = setTimeout(() => refresh(), 2500);
  }

  async function change(item, body, message) {
    try {
      await ui().request("/api/v1/commitments/update", "POST", {id: item.id, ...body});
      if (message) ui().notify(message);
    } catch (error) {
      ui().notify(error.message, true);
    }
    refresh();
    if (window.BridgeBrief) window.BridgeBrief.refresh();
  }

  function scanLine() {
    const scan = data.scan;
    if (scan.running) {
      return scan.total ? `Reading ${scan.checked} of ${scan.total}…` : "Reading your messages…";
    }
    if (!scan.last_scan) return "";
    const minutes = Math.round((Date.now() / 1000 - scan.last_scan) / 60);
    return minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
  }

  function render() {
    const mine = data.mine;
    const theirs = data.theirs;
    $("promises-when").textContent = scanLine();
    $("promises-check").hidden = !data.connected || data.scan.running;
    $("promises-count-mine").textContent = mine.length ? String(mine.length) : "";
    $("promises-count-theirs").textContent = theirs.length + followups.length ? String(theirs.length + followups.length) : "";
    $("promises-tab-mine").classList.toggle("on", tab === "mine");
    $("promises-tab-theirs").classList.toggle("on", tab === "theirs");
    $("promises-tab-mine").setAttribute("aria-selected", String(tab === "mine"));
    $("promises-tab-theirs").setAttribute("aria-selected", String(tab === "theirs"));

    const list = $("promises-list");
    list.replaceChildren();
    const items = tab === "mine" ? mine : theirs;
    for (const item of items.slice(0, 8)) list.append(row(item));
    if (tab === "theirs") for (const item of followups.slice(0, 4)) list.append(noReply(item));

    const empty = $("promises-empty");
    const nothing = !items.length && !(tab === "theirs" && followups.length);
    empty.hidden = !nothing;
    if (nothing) {
      empty.textContent = !data.connected ? "Connect Gmail or Slack, and Bridge keeps track of what you promise and what you're owed."
        : data.scan.running ? "Looking through the last two weeks of your email and Slack…"
        : !data.scan.last_scan ? "Bridge will look for promises in what you send and receive."
        : tab === "mine" ? "Nothing you've promised is open. Nice." : "Nobody owes you anything right now.";
    }
    if (data.scan.error) {
      empty.hidden = false;
      empty.textContent = data.scan.error;
    }

    const closed = data.closed_by_bridge;
    $("promises-closed").hidden = !closed.length;
    $("promises-closed").replaceChildren();
    if (closed.length) {
      const first = closed[0];
      $("promises-closed").append(
        node("span", `✓ Done, from a later message: ${first.what}${closed.length > 1 ? ` (+${closed.length - 1})` : ""}. `),
        button("Undo", "link", () => change(first, {status: "open"}, "Back on the list.")),
      );
    }
  }

  function row(item) {
    const li = node("li", undefined, "promise");
    const main = node("div", undefined, "promise-main");
    main.append(node("span", item.what, "promise-what"));
    const meta = node("span", undefined, "promise-meta");
    const source = item.source === "slack" ? "Slack" : item.source === "email" ? "Email" : "Added by you";
    meta.append(node("span", item.direction === "mine" ? `to ${item.person}` : item.person),
      node("span", ` · ${source} · ${ago(item.said_at)}`, "faint"));
    if (item.followed_up) meta.append(node("span", ` · followed up ${ago(item.followed_up)}`, "faint"));
    main.append(meta);
    if (item.quote) main.title = `“${item.quote}”`;

    const due = dueLabel(item.due);
    const end = node("div", undefined, "promise-end");
    if (due) end.append(node("span", due[0], `due ${due[1]}`));
    const done = button("✓", "promise-done", () => change(item, {status: "done"}, item.direction === "mine" ? "Done. Nice." : "Marked as received."),
      item.direction === "mine" ? "Mark done" : "Mark as received");
    end.append(done, menu(item));
    li.append(main, end);

    if (item.direction === "theirs" && item.can_follow_up) {
      const late = item.due && item.due < data.today;
      const follow = button(open && open.id === item.id ? "Close" : late ? "Follow up" : "Write a follow-up", late ? "secondary small" : "link small",
        () => toggleEditor(item));
      const holder = node("div", undefined, "promise-follow");
      holder.append(follow);
      li.append(holder);
    }
    if (open && open.id === item.id) li.append(editor(item));
    return li;
  }

  function noReply(item) {
    const li = node("li", undefined, "promise quiet");
    const main = node("div", undefined, "promise-main");
    main.append(node("span", `Reply about ${item.about}`, "promise-what"),
      node("span", `${item.name} · you emailed, no reply yet`, "promise-meta"));
    const end = node("div", undefined, "promise-end");
    end.append(node("span", item.status === "overdue" ? "overdue" : `by ${new Date(item.due).toLocaleDateString([], {weekday: "short"})}`,
      item.status === "overdue" ? "due late" : "due"));
    li.append(main, end);
    return li;
  }

  function menu(item) {
    const wrap = node("details", undefined, "promise-menu");
    const summary = node("summary", "⋯");
    summary.setAttribute("aria-label", "More");
    wrap.append(summary);
    const panel = node("div", undefined, "promise-menu-panel");
    const date = node("input");
    date.type = "date";
    date.value = item.due || "";
    date.setAttribute("aria-label", "Due date");
    date.addEventListener("change", () => change(item, {due: date.value || ""}, date.value ? "Date changed." : "Date removed."));
    const dateRow = node("label", "Due ", "promise-date");
    dateRow.append(date);
    panel.append(dateRow);
    if (item.link) {
      const link = node("a", item.source === "slack" ? "Open in Slack" : "Open in Gmail");
      link.href = item.link;
      link.target = "_blank";
      link.rel = "noopener";
      panel.append(link);
    }
    panel.append(button("Not a promise", "link", () => change(item, {status: "dismissed"}, "Removed. Bridge won't show it again.")));
    wrap.append(panel);
    return wrap;
  }

  // Following up -------------------------------------------------------------------------------

  async function toggleEditor(item) {
    if (open && open.id === item.id) {
      open = null;
      render();
      return;
    }
    open = {id: item.id, body: "", loading: true};
    render();
    try {
      const draft = await ui().request("/api/v1/commitments/draft", "POST", {id: item.id, name: ui().owner().name || ""});
      if (open && open.id === item.id) open = {id: item.id, body: draft.body, loading: false};
    } catch (error) {
      ui().notify(error.message, true);
      open = null;
    }
    render();
  }

  function editor(item) {
    const box = node("div", undefined, "promise-editor");
    const where = item.source === "slack" ? "in the same Slack conversation" : "as a reply in the same email thread";
    box.append(node("p", `To ${item.person}, ${where}`, "hint"));
    const text = node("textarea");
    text.rows = 4;
    text.maxLength = 8000;
    text.value = open.loading ? "" : open.body;
    text.placeholder = open.loading ? "Writing it in your style…" : "";
    text.disabled = open.loading;
    text.setAttribute("aria-label", "Follow-up message");
    text.addEventListener("input", () => { open.body = text.value; });
    const actions = node("div", undefined, "row-actions");
    const send = button("Send", "primary small", async () => {
      const body = text.value.trim();
      if (!body) return;
      send.disabled = true;
      try {
        const sent = await ui().request("/api/v1/commitments/send", "POST", {id: item.id, body});
        ui().notify(`Sent to ${sent.to}.`);
        open = null;
      } catch (error) {
        ui().notify(error.message, true);
        send.disabled = false;
        return;
      }
      refresh();
    });
    send.disabled = open.loading;
    actions.append(send, button("Cancel", "ghost small", () => { open = null; render(); }));
    box.append(text, actions);
    return box;
  }

  // Events -------------------------------------------------------------------------------------

  $("promises-tab-mine").addEventListener("click", () => { tab = "mine"; open = null; render(); });
  $("promises-tab-theirs").addEventListener("click", () => { tab = "theirs"; open = null; render(); });
  $("promises-check").addEventListener("click", async () => {
    try {
      await ui().request("/api/v1/commitments/scan", "POST");
    } catch (error) {
      ui().notify(error.message, true);
    }
    refresh();
  });

  // A menu closes when you click anywhere else.
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".promise-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });

  // Opens a tab of the card and brings it into view (from the brief).
  function show(which) {
    tab = which === "theirs" ? "theirs" : "mine";
    open = null;
    if (data) render();
    $("promises-card").scrollIntoView({behavior: "smooth", block: "center"});
  }

  window.BridgePromises = {refresh, show};
})();
