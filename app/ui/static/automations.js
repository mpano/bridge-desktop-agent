"use strict";

// Automations: routines with switches, "tell me when…" alerts, scheduled requests and the
// replies you're waiting on. New ones are described in plain words and created by Bridge.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let data = null;

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

  async function refresh() {
    try {
      data = await ui().request("/api/v1/automations");
      render();
    } catch (error) {
      ui().notify(error.message, true);
    }
  }

  async function save(changes) {
    try {
      const result = await ui().request("/api/v1/proactive/settings", "POST", changes);
      data.settings = result.settings;
      render();
    } catch (error) {
      ui().notify(error.message, true);
      refresh();
    }
  }

  // Routines -----------------------------------------------------------------------------------

  function toggle(label, on, onChange) {
    const control = button("", on ? "switch on" : "switch", () => onChange(!on));
    control.setAttribute("role", "switch");
    control.setAttribute("aria-checked", String(on));
    control.setAttribute("aria-label", label);
    control.append(node("span", undefined, "knob"));
    return control;
  }

  function timeInput(label, value, onChange) {
    const input = node("input");
    input.type = "time";
    input.value = value || "";
    input.setAttribute("aria-label", label);
    input.addEventListener("change", () => { if (input.value) onChange(input.value); });
    return input;
  }

  function routine(title, text, on, key, setting) {
    const card = node("div", undefined, `card routine${on === null ? "" : on ? " active" : ""}`);
    const top = node("div", undefined, "routine-top");
    const words = node("div", undefined, "routine-words");
    words.append(node("h3", title), node("p", text));
    top.append(words);
    if (on !== null) top.append(toggle(title, on, (value) => save({[key]: value})));
    card.append(top);
    if (setting) card.append(setting);
    return card;
  }

  function renderRoutines() {
    const s = data.settings;
    const grid = $("routines");
    grid.replaceChildren();

    const morning = node("label", undefined, "routine-setting");
    morning.append(node("span", "Weekdays at"), timeInput("Morning brief time", s.morning_time, (value) => save({morning_time: value})));
    grid.append(routine("Morning brief", "Each weekday morning, Bridge plans your day and picks your 3 most important things — promises due, people waiting on you, urgent email and Slack.", s.morning_plan, "morning_plan", morning));

    const lead = node("label", undefined, "routine-setting");
    const minutes = node("select");
    minutes.setAttribute("aria-label", "Minutes before");
    for (const value of [5, 10, 15, 30]) {
      const option = node("option", `${value} minutes before`);
      option.value = String(value);
      option.selected = value === s.lead_minutes;
      minutes.append(option);
    }
    if (![5, 10, 15, 30].includes(s.lead_minutes)) {
      const option = node("option", `${s.lead_minutes} minutes before`);
      option.value = String(s.lead_minutes);
      option.selected = true;
      minutes.append(option);
    }
    minutes.addEventListener("change", () => save({lead_minutes: Number(minutes.value)}));
    lead.append(minutes);
    grid.append(routine("Meeting heads-up", "A notification before each meeting, with when it starts and where.", s.meeting_prep, "meeting_prep", lead));

    const evening = node("label", undefined, "routine-setting");
    evening.append(node("span", "Weekdays at"), timeInput("Evening wrap-up time", s.evening_time, (value) => save({evening_time: value})));
    grid.append(routine("Evening wrap-up", "What got done, what slipped (move it to tomorrow with one tap) and tomorrow at a glance, so you can close the day.", s.evening_summary, "evening_summary", evening));

    const hours = node("div", undefined, "routine-setting");
    const from = timeInput("Workday starts", s.work_start, (value) => save({work_start: value}));
    const to = timeInput("Workday ends", s.work_end, (value) => save({work_end: value}));
    hours.append(node("span", "From"), from, node("span", "to"), to);
    grid.append(routine("Working hours", "Plans, focus blocks and heads-ups stay inside these hours.", null, null, hours));
  }

  // Lists --------------------------------------------------------------------------------------

  function empty(list, text) {
    list.append(node("li", text, "line-empty"));
  }

  const SOURCES = {email: "Email", slack: "Slack", github_pr: "GitHub", github_release: "GitHub", jira: "Jira", app: "App"};
  const EXAMPLES = ["PR 412 is merged", "ABC-9 moves to QA", "Sarah replies to me", "someone mentions me on Slack", "a new release of owner/repo"];

  function ago(seconds) {
    const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const when = new Date(seconds * 1000);
    const today = new Date().toDateString() === when.toDateString();
    return today ? when.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})
      : when.toLocaleDateString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
  }

  function renderWatches() {
    const list = $("watches");
    list.replaceChildren();
    const order = {active: 0, done: 1, expired: 2};
    const watches = [...data.watches].sort((a, b) => (order[a.status] - order[b.status]) || (b.id - a.id));
    for (const watch of watches) {
      const row = node("li", undefined, `line watch-line ${watch.status}`);
      row.append(node("span", SOURCES[watch.kind] || watch.kind, `src ${watch.kind}`));
      const words = node("span", undefined, "line-main");
      const title = watch.url ? window.BridgeDOM.link(watch.label, watch.url, "watch-title") : node("span", watch.label, "watch-title");
      words.append(title);
      if (watch.status === "done") words.append(node("span", watch.fired || "It happened.", "watch-fired"));
      else if (watch.status === "expired") words.append(node("span", "Stopped: it didn't happen in time.", "faint"));
      else if (watch.fired) words.append(node("span", `Last: ${watch.fired}`, "faint"));
      else words.append(node("span", watch.query, "mono faint"));
      row.append(words);
      let status;
      if (watch.status === "done") status = node("span", `✓ ${ago(watch.fired_at)}`, "pill good");
      else if (watch.status === "expired") status = node("span", "stopped", "mono faint");
      else if (watch.last_error) status = node("span", watch.last_error, "line-error");
      else status = node("span", watch.once ? "waiting" : "every time", "pill pending");
      status.title = watch.last_checked ? `Checked ${ago(watch.last_checked)}` : "Starting";
      row.append(status, button(watch.status === "active" ? "Stop" : "Clear", "ghost small", () => remove("/api/v1/watches/remove", watch.id)));
      list.append(row);
    }
    if (!watches.length) empty(list, "Nothing yet. Try “PR 412 is merged” or “Sarah replies to me”.");
    const examples = $("watch-examples");
    if (examples && !examples.childElementCount) {
      for (const text of EXAMPLES) {
        examples.append(button(text, "chip", () => {
          $("watch-new-query").value = text;
          $("watch-new-query").focus();
        }));
      }
    }
  }

  function renderSchedules() {
    const list = $("schedules");
    list.replaceChildren();
    for (const item of data.schedules) {
      const row = node("li", undefined, "line");
      row.append(node("span", item.when, "mono when"), node("span", item.message, "line-main"),
        button("Remove", "ghost small", () => remove("/api/v1/schedules/delete", item.id)));
      list.append(row);
    }
    if (!data.schedules.length) empty(list, "Nothing scheduled. Try “every Monday at 9, brief me on the week”.");
  }

  function renderFollowups() {
    const list = $("followups");
    list.replaceChildren();
    for (const item of data.followups) {
      const row = node("li", undefined, "line");
      const words = node("span", undefined, "line-main");
      words.append(node("strong", item.name), node("span", item.about, "faint"));
      const due = new Date(item.due);
      const label = item.status === "replied" ? "replied" : item.status === "overdue" ? "overdue"
        : `by ${due.toLocaleDateString([], {weekday: "short"})} ${due.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false})}`;
      const tone = item.status === "replied" ? "pill good" : item.status === "overdue" ? "pill warm" : "mono faint";
      row.append(words, node("span", label, tone));
      if (item.status !== "replied") row.append(button("Cancel", "ghost small", () => remove("/api/v1/followups/cancel", item.id)));
      list.append(row);
    }
    if (!data.followups.length) empty(list, "Not waiting on anyone. Send a reply from Inbox with the reminder on.");
  }

  async function remove(path, id) {
    try {
      await ui().request(path, "POST", {id});
    } catch (error) {
      ui().notify(error.message, true);
    }
    refresh();
  }

  function render() {
    if (!data) return;
    renderRoutines();
    renderWatches();
    renderSchedules();
    renderFollowups();
  }

  // Events -------------------------------------------------------------------------------------

  $("auto-new").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("auto-input").value.trim();
    if (!text || ui().busy()) return;
    $("auto-input").value = "";
    ui().ask(text);
  });

  $("watch-new").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("watch-new-query");
    const text = input.value.trim();
    if (!text) return;
    const submit = $("watch-submit");
    submit.disabled = true;
    submit.textContent = "Setting up…";
    try {
      const made = await ui().request("/api/v1/watches/ask", "POST", {text});
      input.value = "";
      ui().notify(made.status === "done" ? `It already happened: ${made.fired}` : `Watching: ${made.label}`);
      refresh();
    } catch (error) {
      ui().notify(error.message, true);
    } finally {
      submit.disabled = false;
      submit.textContent = "Watch";
    }
  });

  window.BridgeAutomations = {refresh};
})();
