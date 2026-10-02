"use strict";

// Settings: one place for shortcuts, voice, privacy, Mac permissions, account, projects and
// diagnostics. Changes to the app (not the account) are saved to .env and need a restart,
// which the banner offers.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let section = "general";
  let data = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function row(name, hint, control) {
    const line = node("div", undefined, "set");
    const words = node("div", undefined, "set-words");
    words.append(node("span", name, "set-name"));
    if (hint) words.append(node("span", hint, "set-hint"));
    line.append(words);
    if (control) line.append(control);
    return line;
  }

  function toggle(label, on, onChange) {
    const control = node("button", undefined, on ? "switch on" : "switch");
    control.type = "button";
    control.setAttribute("role", "switch");
    control.setAttribute("aria-checked", String(on));
    control.setAttribute("aria-label", label);
    control.append(node("span", undefined, "knob"));
    control.addEventListener("click", () => onChange(!on));
    return control;
  }

  function segmented(label, options, value, onChange) {
    const group = node("div", undefined, "seg");
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", label);
    for (const [key, text] of options) {
      const option = node("button", text, key === value ? "on" : "");
      option.type = "button";
      option.setAttribute("role", "radio");
      option.setAttribute("aria-checked", String(key === value));
      option.addEventListener("click", () => { if (key !== value) onChange(key); });
      group.append(option);
    }
    return group;
  }

  function pill(text, tone) {
    return node("span", text, `pill-state ${tone}`);
  }

  const keys = (...parts) => {
    const box = node("span", undefined, "keys");
    for (const part of parts) box.append(node("kbd", part));
    return box;
  };

  // Sections -----------------------------------------------------------------------------------

  function show(name) {
    section = name || "general";
    document.querySelectorAll(".settings-nav [data-section]").forEach((button) => {
      if (button.dataset.section === section) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    document.querySelectorAll(".settings-section").forEach((panel) => { panel.hidden = panel.dataset.panel !== section; });
    if (section === "account") ui().refreshAccount();
    refresh();
  }

  async function refresh() {
    try {
      data = await ui().request("/api/v1/settings/app");
      render();
    } catch (error) {
      ui().notify(error.message, true);
    }
  }

  async function save(changes) {
    try {
      const result = await ui().request("/api/v1/settings/app", "POST", changes);
      $("restart-banner").hidden = !result.restart_needed;
      await refresh();
    } catch (error) {
      ui().notify(error.message, true);
    }
  }

  function renderShortcuts() {
    const s = data.shortcuts;
    $("settings-shortcuts").replaceChildren(
      row("Talk to Bridge", "Starts listening right away, from any app.", node("span", undefined, "set-end")),
      row("Act on what you see", "Summarize, translate or rewrite the selected text — or the whole window.", node("span", undefined, "set-end")),
      row("Dictate", "Speak, and Bridge types clean text where your cursor is.", node("span", undefined, "set-end")),
    );
    const rows = $("settings-shortcuts").children;
    rows[0].lastChild.append(keys("⌘", "⇧", "Space"), toggle("Talk to Bridge shortcut", s.voice, (on) => save({voice_shortcut: on})));
    rows[1].lastChild.append(keys("⌃", "⌥", "Space"), toggle("Act on what you see shortcut", s.text_actions, (on) => save({text_actions_shortcut: on})));
    rows[2].lastChild.append(keys("⌃", "⌥", "D"), toggle("Dictate shortcut", s.dictation, (on) => save({dictation_shortcut: on})));
  }

  function renderVoice() {
    const v = data.voice;
    const pause = node("select", undefined, "small-select");
    pause.setAttribute("aria-label", "Stop after a pause of");
    for (const value of [1.5, 2, 2.5, 3, 4, 5]) {
      const option = node("option", `${value} seconds`);
      option.value = String(value);
      option.selected = value === v.dictation_pause_seconds;
      pause.append(option);
    }
    pause.addEventListener("change", () => save({dictation_pause_seconds: Number(pause.value)}));
    $("settings-voice").replaceChildren(
      row("Speech to text", v.speech === "local" ? "On this Mac with Whisper. Your voice never leaves the Mac." : "OpenAI transcribes your recordings.",
        segmented("Speech to text", [["local", "On this Mac"], ["openai", "OpenAI"]], v.speech, (key) => save({speech: key}))),
      row("Tidy up dictation", "Removes “um”s and applies spoken corrections. Sends the text, never audio, to OpenAI.",
        toggle("Tidy up dictation", v.dictation_cleanup, (on) => save({dictation_cleanup: on}))),
      row("Stop dictating after a pause of", "", pause),
    );
  }

  function renderPrivacy() {
    const p = data.privacy;
    const stays = () => pill("stays on this Mac", "good");
    const goes = () => pill("sent to OpenAI", "warm");
    $("settings-privacy").replaceChildren(
      row("What you ask", "Typed or spoken requests, as text, and what Bridge remembers about you.", goes()),
      row("Results from email, calendar and Slack", p.share_results ? "OpenAI sees them so it can answer about them." : "Off: you still see them here, but OpenAI only learns whether a step worked.",
        toggle("Share results with OpenAI", p.share_results, (on) => save({share_results: on}))),
      row("What's on your screen", p.share_screen ? "Only when you say “this”, and only the window behind Bridge." : "Off: Bridge can read the window for you, but not tell OpenAI what it says.",
        toggle("Share screen content with OpenAI", p.share_screen, (on) => save({share_screen: on}))),
      row("Your voice", data.voice.speech === "local" ? "Transcribed on this Mac, then deleted." : "Sent to OpenAI for transcription.", data.voice.speech === "local" ? stays() : goes()),
      row("Sign-ins, history and files", "Account tokens live in your Mac's Keychain.", stays()),
      row("Model", p.openai_key ? `OpenAI · ${p.model}` : "No OpenAI key yet — add it to .env.", pill(p.openai_key ? "key in .env" : "missing", p.openai_key ? "" : "warm")),
    );

    const blocked = $("settings-blocked");
    blocked.replaceChildren();
    const list = node("div", undefined, "chips-wrap");
    for (const name of p.always_blocked) list.append(node("span", name, "chip-static"));
    for (const name of p.blocked_apps) {
      const chip = node("span", name, "chip-static mine");
      const remove = node("button", "×", "chip-x");
      remove.type = "button";
      remove.setAttribute("aria-label", `Stop blocking ${name}`);
      remove.addEventListener("click", () => save({blocked_apps: p.blocked_apps.filter((item) => item !== name)}));
      chip.append(remove);
      list.append(chip);
    }
    const form = node("form", undefined, "inline-add");
    const input = node("input");
    input.placeholder = "Add an app, e.g. Banking";
    input.maxLength = 80;
    input.setAttribute("aria-label", "App to block");
    const add = node("button", "Add", "secondary small");
    add.type = "submit";
    form.append(input, add);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (name) save({blocked_apps: [...p.blocked_apps, name]});
    });
    const words = node("div", undefined, "set-words");
    words.append(node("span", "Apps Bridge never reads", "set-name"), node("span", "Password managers are always on this list. Add any app you want kept private.", "set-hint"));
    const block = node("div", undefined, "set column");
    block.append(words, list, form);
    blocked.append(block);
  }

  function renderPermissions() {
    const box = $("settings-permissions");
    box.replaceChildren();
    if (!data.permissions.length) {
      box.append(row("Not on a Mac", "Permissions only apply to the Mac app.", null));
      return;
    }
    const labels = {allowed: ["Allowed", "good"], denied: ["Off", "warm"], not_asked: ["Not asked yet", ""], unknown: ["Unknown", ""]};
    for (const item of data.permissions) {
      const [text, tone] = labels[item.state] || labels.unknown;
      const end = node("span", undefined, "set-end");
      end.append(pill(text, tone));
      if (item.state !== "allowed") {
        const open = node("a", "Open Settings", "secondary small button-link");
        open.href = item.settings_url;
        end.append(open);
      }
      box.append(row(item.name, item.why, end));
    }
  }

  function render() {
    if (!data) return;
    $("restart-banner").hidden = !data.restart_needed;
    $("restart-now").hidden = !data.can_restart;
    renderShortcuts();
    renderVoice();
    renderPrivacy();
    renderPermissions();
  }

  document.querySelectorAll(".settings-nav [data-section]").forEach((button) => {
    button.addEventListener("click", () => show(button.dataset.section));
  });
  $("test-notification").addEventListener("click", async () => {
    try {
      await ui().request("/api/v1/notifications/test", "POST");
      ui().notify("Sent. If nothing appears, allow Bridge in System Settings › Notifications.");
    } catch (error) {
      ui().notify(error.message, true);
    }
  });
  $("restart-now").addEventListener("click", async () => {
    try {
      await ui().request("/api/v1/app/restart", "POST");
      ui().notify("Restarting Bridge… this window reopens in a few seconds.");
    } catch (error) {
      ui().notify(error.message, true);
    }
  });

  window.BridgeSettings = {show, refresh, current: () => section};
})();
