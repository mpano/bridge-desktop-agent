"use strict";

// First-launch setup, step by step: welcome, the OpenAI key (into the Keychain), Mac
// permissions (real macOS prompts), apps, shortcut practice and your days. Steps that can
// change outside this page (permissions, sign-ins, shortcuts) refresh themselves.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  const STEPS = ["Welcome", "Model", "Mac permissions", "Your apps", "Shortcuts", "Your days", "Done"];
  let step = 0;
  let state = null;
  let poll = null;
  const tried = new Set();

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

  const ok = (text) => {
    const line = node("p", undefined, "ob-ok");
    line.append(node("span", "✓", "tick"), node("span", text));
    return line;
  };

  async function load() {
    try {
      state = await ui().request("/api/v1/onboarding");
    } catch (error) {
      ui().notify(error.message, true);
    }
  }

  async function startIfNeeded() {
    await load();
    if (state && !state.done) start();
    else ui().view("today");
  }

  async function start() {
    step = 0;
    if (!state) await load();
    ui().view("onboarding");
    render();
  }

  async function finish(then) {
    clearInterval(poll);
    try { await ui().request("/api/v1/onboarding/done", "POST", {done: true}); } catch { /* still leave setup */ }
    ui().view("today");
    if (then) ui().ask(then);
  }

  function go(next) {
    step = Math.max(0, Math.min(STEPS.length - 1, next));
    render();
  }

  // Steps ---------------------------------------------------------------------------------------

  function heading(label, title, lede) {
    const parts = [node("p", label, "label"), node("h1", title, step === 0 || step === STEPS.length - 1 ? "ob-hero" : "ob-title")];
    if (lede) parts.push(node("p", lede, "ob-lede"));
    parts[1].id = "ob-title";
    return parts;
  }

  function welcome(body) {
    body.append(...heading("Welcome", "Meet Bridge.", "Ask in plain words and Bridge gets it done across your Mac — email, calendar, Slack, music. It always asks before it sends or changes anything."));
    const points = node("div", undefined, "ob-points");
    for (const [n, strong, rest] of [
      ["1", "Say it how you'd say it.", " “Plan my day.” “Reply to this.” “Focus for an hour.”"],
      ["2", "It works where you work.", " From any app, with a shortcut or your voice."],
      ["3", "You stay in charge.", " Bridge shows you exactly what it will do, and waits."],
    ]) {
      const point = node("div", undefined, "ob-point");
      const text = node("p");
      text.append(node("strong", strong), document.createTextNode(rest));
      point.append(node("span", n, "ob-num"), text);
      points.append(point);
    }
    body.append(points);
  }

  function model(body) {
    body.append(...heading(`Step 1 of ${STEPS.length - 2}`, "Connect a model", "Bridge uses OpenAI to understand what you ask. Your key is kept in your Mac's Keychain, never in a file."));
    const o = state.openai;
    if (o.set) {
      body.append(ok(o.source === "keychain" ? "Your OpenAI key is set, in the Keychain." : "Your OpenAI key is set, in your .env file."));
      if (o.source !== "keychain") {
        body.append(node("p", "Move it to the Keychain so it isn't sitting in a plain text file. Bridge keeps working the same.", "ob-note"),
          button("Move it to the Keychain", "secondary", async () => {
            try {
              await ui().request("/api/v1/onboarding/openai-key/move", "POST");
              await load();
              render();
            } catch (error) { ui().notify(error.message, true); }
          }));
      }
      return;
    }
    const field = node("label", undefined, "ob-field");
    const input = node("input");
    input.type = "password";
    input.placeholder = "sk-…";
    input.autocomplete = "off";
    input.spellcheck = false;
    field.append(node("span", "OpenAI API key"), input);
    const save = button("Check and save", "primary", async () => {
      save.disabled = true;
      save.textContent = "Checking…";
      try {
        await ui().request("/api/v1/onboarding/openai-key", "POST", {key: input.value.trim()});
        input.value = "";
        await load();
        render();
      } catch (error) {
        ui().notify(error.message, true);
        save.disabled = false;
        save.textContent = "Check and save";
      }
    });
    const link = node("a", "Get a key from OpenAI ↗", "ob-link");
    link.href = "https://platform.openai.com/api-keys";
    link.target = "_blank";
    link.rel = "noopener";
    const row = node("div", undefined, "ob-row");
    row.append(save, link);
    body.append(field, row, node("p", "Bridge checks the key with OpenAI before saving it. It's sent nowhere else.", "ob-note"));
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") save.click(); });
  }

  function permissions(body) {
    body.append(...heading(`Step 2 of ${STEPS.length - 2}`, "Let Bridge help on your Mac", "macOS asks you about each one. Allow what you want — Bridge tells you if something needs a permission later."));
    const list = node("div", undefined, "card group");
    for (const item of state.permissions) {
      const row = node("div", undefined, "set");
      const words = node("div", undefined, "set-words");
      words.append(node("span", item.name, "set-name"), node("span", item.why, "set-hint"));
      row.append(words);
      if (item.state === "allowed") row.append(node("span", "✓ Allowed", "pill-state good"));
      else if (item.state === "denied") {
        const open = node("a", "Open Settings", "secondary small button-link");
        open.href = item.settings_url;
        row.append(open);
      } else {
        row.append(button("Allow", "secondary small", async () => {
          try { await ui().request("/api/v1/permissions/request", "POST", {key: item.key}); } catch (error) { ui().notify(error.message, true); }
        }));
      }
      list.append(row);
    }
    if (!state.permissions.length) list.append(node("p", "Permissions only apply to the Mac app.", "empty"));
    body.append(list);
  }

  function apps(body) {
    body.append(...heading(`Step 3 of ${STEPS.length - 2}`, "Connect your apps", "Optional. Each one opens its own sign-in page in your browser, and you can disconnect any time."));
    const c = state.connections;
    if (!c.enabled) {
      body.append(node("p", "Connected services are turned off on this Mac. You can set them up later in Connections.", "ob-note"));
      return;
    }
    const grid = node("div", undefined, "ob-apps");
    for (const provider of c.providers) {
      const card = node("div", undefined, "card ob-app");
      card.append(node("strong", provider.name));
      if (provider.connected) card.append(node("span", "✓ Connected", "pill-state good"));
      else if (!provider.configured) card.append(node("span", "Needs setup", "pill-state"));
      else card.append(button("Connect", "secondary small", async () => {
        try {
          const result = await ui().request("/api/v1/connections/start", "POST", {provider: provider.provider, allow_actions: true});
          window.open(result.authorization_url, "_blank", "noopener");
        } catch (error) { ui().notify(error.message, true); }
      }));
      grid.append(card);
    }
    body.append(grid);
  }

  function shortcuts(body) {
    body.append(...heading(`Step 4 of ${STEPS.length - 2}`, "Three shortcuts, from any app", "Try each one now — it's the fastest way to remember them. They tick themselves when Bridge sees you use them."));
    const used = state.usage || {};
    for (const [key, combo, name, task] of [
      ["talk", "⌘⇧Space", "Talk to Bridge", "Press it and say “what's on my calendar today?”"],
      ["act", "⌃⌥Space", "Act on what you see", "Select some text anywhere, press it, then choose Summarize."],
      ["dictate", "⌃⌥D", "Dictate anywhere", "Click into a text box, press it and say a sentence."],
    ]) {
      const done = Boolean(used[key]) || tried.has(key);
      const row = node("div", undefined, `ob-try${done ? " tried" : ""}`);
      const words = node("div", undefined, "set-words");
      words.append(node("span", name, "set-name"), node("span", task, "set-hint"));
      row.append(node("span", combo, "ob-combo"), words,
        done ? node("span", "✓", "tick big") : button("I tried it", "ghost small", () => { tried.add(key); render(); }));
      body.append(row);
    }
  }

  function days(body) {
    body.append(...heading(`Step 5 of ${STEPS.length - 2}`, "Your days", "Bridge plans inside your working hours and can have your day ready each morning."));
    const d = state.days;
    const save = async (changes) => {
      try {
        await ui().request("/api/v1/proactive/settings", "POST", changes);
        await load();
        render();
      } catch (error) { ui().notify(error.message, true); }
    };
    const time = (label, value, key) => {
      const input = node("input");
      input.type = "time";
      input.value = value;
      input.setAttribute("aria-label", label);
      input.addEventListener("change", () => { if (input.value) save({[key]: input.value}); });
      return input;
    };
    const flip = (label, on, key) => {
      const control = button("", on ? "switch on" : "switch", () => save({[key]: !on}));
      control.setAttribute("role", "switch");
      control.setAttribute("aria-checked", String(on));
      control.setAttribute("aria-label", label);
      control.append(node("span", undefined, "knob"));
      return control;
    };
    const card = node("div", undefined, "card group");
    const hours = node("div", undefined, "set");
    const hoursWords = node("div", undefined, "set-words");
    hoursWords.append(node("span", "Working hours", "set-name"));
    const hoursEnd = node("span", undefined, "set-end");
    hoursEnd.append(time("Workday starts", d.work_start, "work_start"), node("span", "to", "faint"), time("Workday ends", d.work_end, "work_end"));
    hours.append(hoursWords, hoursEnd);
    const morning = node("div", undefined, "set");
    const morningWords = node("div", undefined, "set-words");
    morningWords.append(node("span", "Plan my day every weekday morning", "set-name"), node("span", `Ready at ${d.morning_time}, with a notification.`, "set-hint"));
    morning.append(morningWords, flip("Plan my day every weekday morning", d.morning_plan, "morning_plan"));
    const meetings = node("div", undefined, "set");
    const meetingWords = node("div", undefined, "set-words");
    meetingWords.append(node("span", "Heads-up before meetings", "set-name"), node("span", "A notification shortly before each meeting.", "set-hint"));
    meetings.append(meetingWords, flip("Heads-up before meetings", d.meeting_prep, "meeting_prep"));
    card.append(hours, morning, meetings);
    body.append(card);
  }

  function done(body) {
    const name = (ui().owner().name || "").trim().split(/\s+/)[0];
    body.append(...heading("All set", name ? `You're ready, ${name}.` : "You're ready.", "Try one of these to start — or press ⌘⇧Space anywhere and just say it."));
    const chips = node("div", undefined, "chips");
    chips.append(button("Plan my day", "", () => finish("Plan my day")),
      button("What needs my attention?", "", () => finish("What needs my attention?")),
      button("Focus for 90 minutes", "", () => finish("Focus for 90 minutes")));
    body.append(chips);
  }

  // Render ---------------------------------------------------------------------------------------

  function render() {
    if (!state) return;
    const rail = $("ob-steps");
    rail.replaceChildren();
    STEPS.forEach((name, index) => {
      const item = node("li", undefined, `ob-step${index === step ? " now" : index < step ? " done" : ""}`);
      if (index === step) item.setAttribute("aria-current", "step");
      item.append(node("span", index < step ? "✓" : String(index + 1), "dot"), node("span", name));
      rail.append(item);
    });
    const body = $("ob-body");
    body.replaceChildren();
    [welcome, model, permissions, apps, shortcuts, days, done][step](body);
    const last = step === STEPS.length - 1;
    $("ob-back").hidden = step === 0 || last;
    $("ob-later").hidden = ![3, 4].includes(step);
    $("ob-next").textContent = step === 0 ? "Get started" : last ? "Open Today" : step === STEPS.length - 2 ? "Finish" : "Continue";
    // Permissions, sign-ins and shortcuts change outside this page: keep them current.
    clearInterval(poll);
    if ([2, 3, 4].includes(step)) poll = setInterval(async () => { await load(); if (!$("view-onboarding").hidden) renderLive(); }, 2000);
  }

  function renderLive() {
    const body = $("ob-body");
    const focused = document.activeElement && body.contains(document.activeElement);
    if (focused) return;  // Never redraw under the user's cursor.
    body.replaceChildren();
    [welcome, model, permissions, apps, shortcuts, days, done][step](body);
  }

  $("ob-next").addEventListener("click", () => (step === STEPS.length - 1 ? finish() : go(step + 1)));
  $("ob-back").addEventListener("click", () => go(step - 1));
  $("ob-later").addEventListener("click", () => go(step + 1));
  $("ob-skip").addEventListener("click", () => finish());
  $("rerun-setup").addEventListener("click", () => { state = null; start(); });

  window.BridgeOnboarding = {startIfNeeded, start};
})();
