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
    if (section === "phone" && window.BridgePhone) window.BridgePhone.showSettings();
    if (section === "health") renderHealth();
    if (section === "about") renderUpdates();
    if (section === "usage" && window.BridgeUsage) window.BridgeUsage.show();
    if (section === "learning" && window.BridgeLearning) window.BridgeLearning.show();
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
      row("What's this?", "Point at anything on screen and Bridge explains what it is and what it does.", node("span", undefined, "set-end")),
      row("Bridge on your desk", "Your Bridge in a corner of the screen: next meeting, what needs you, click to talk. Drag it anywhere.", node("span", undefined, "set-end")),
    );
    const rows = $("settings-shortcuts").children;
    rows[0].lastChild.append(keys("⌘", "⇧", "Space"), toggle("Talk to Bridge shortcut", s.voice, (on) => save({voice_shortcut: on})));
    rows[1].lastChild.append(keys("⌃", "⌥", "Space"), toggle("Act on what you see shortcut", s.text_actions, (on) => save({text_actions_shortcut: on})));
    rows[2].lastChild.append(keys("⌃", "⌥", "D"), toggle("Dictate shortcut", s.dictation, (on) => save({dictation_shortcut: on})));
    rows[3].lastChild.append(keys("⌃", "⌥", "W"), toggle("What's this? shortcut", s.explain, (on) => save({explain_shortcut: on})));
    rows[4].lastChild.append(toggle("Bridge on your desk", s.desk_buddy, (on) => save({desk_buddy: on})));
  }

  // Bridge's voice: a card for each voice with its face, how it sounds and a ▶ to hear it.
  // Built once and kept, so a preview keeps playing while the choice is saved.
  let gallery = null;
  let preview = null;  // {voice, audio, context, url, avatar, button}

  function stopPreview() {
    if (!preview) return;
    preview.audio.pause();
    if (preview.context) preview.context.close().catch(() => {});
    URL.revokeObjectURL(preview.url);
    preview.avatar.setState("idle");
    preview.avatar.listenTo("speaking", null);
    preview.button.textContent = "▶";
    preview.button.setAttribute("aria-label", `Hear ${preview.name}`);
    preview.card.classList.remove("playing");
    preview = null;
  }

  async function play(voice, card, face, playButton) {
    const same = preview && preview.voice === voice.id;
    stopPreview();
    if (same) return;
    playButton.textContent = "…";
    let blob;
    try {
      const response = await fetch(`/api/v1/talk/sample/${encodeURIComponent(voice.id)}`, {credentials: "same-origin"});
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : "Couldn't play this voice.");
      }
      blob = await response.blob();
    } catch (error) {
      playButton.textContent = "▶";
      ui().notify(error.message || "Couldn't play this voice.", true);
      return;
    }
    stopPreview();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    preview = {voice: voice.id, name: voice.name, audio, url, avatar: face, button: playButton, card, context: null};
    if (window.AudioContext) {
      try {
        const context = new AudioContext();
        const source = context.createMediaElementSource(audio);
        source.connect(context.destination);
        face.listenTo("speaking", window.BridgeAvatar.meter(context, source));
        preview.context = context;
      } catch { /* the face still moves, just not with the sound */ }
    }
    face.setState("speaking");
    card.classList.add("playing");
    playButton.textContent = "■";
    playButton.setAttribute("aria-label", `Stop ${voice.name}`);
    audio.addEventListener("ended", () => { if (preview && preview.audio === audio) stopPreview(); });
    audio.play().catch(() => stopPreview());
  }

  function voiceGallery(current) {
    if (!gallery) {
      gallery = node("div", undefined, "set set-block");
      const words = node("div", undefined, "set-words");
      words.append(node("span", "Bridge's voice", "set-name"),
        node("span", "Tap ▶ to hear a voice. Your choice is used from the next conversation.", "set-hint"));
      const grid = node("div", undefined, "voice-grid");
      grid.setAttribute("role", "radiogroup");
      grid.setAttribute("aria-label", "Bridge's voice");
      gallery.append(words, grid);
      ui().request("/api/v1/talk/voices").then((found) => {
        for (const voice of found.voices) {
          const card = node("div", undefined, "voice-card");
          card.dataset.voice = voice.id;
          card.tabIndex = 0;
          card.setAttribute("role", "radio");
          const faceBox = node("div", undefined, "voice-face");
          const face = window.BridgeAvatar.create(faceBox, voice.colors, {size: 56});
          const text = node("div", undefined, "voice-words");
          const title = node("div", undefined, "voice-title");
          title.append(node("strong", voice.name));
          if (voice.recommended) title.append(node("span", "Recommended", "voice-tag"));
          text.append(title, node("span", voice.about, "voice-about"));
          const playButton = node("button", "▶", "voice-play");
          playButton.type = "button";
          playButton.setAttribute("aria-label", `Hear ${voice.name}`);
          playButton.addEventListener("click", (event) => {
            event.stopPropagation();
            play(voice, card, face, playButton);
          });
          const pick = () => {
            if (card.classList.contains("chosen")) return;
            markVoice(voice.id);
            save({talk_voice: voice.id});
          };
          card.addEventListener("click", pick);
          card.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              pick();
            }
          });
          card.append(faceBox, text, playButton);
          grid.append(card);
        }
        markVoice(gallery.dataset.current);
      }).catch(() => grid.replaceChildren(node("p", "Couldn't load the voices.", "set-hint")));
    }
    markVoice(current);
    return gallery;
  }

  function markVoice(current) {
    if (!gallery) return;
    gallery.dataset.current = current;
    for (const card of gallery.querySelectorAll(".voice-card")) {
      const chosen = card.dataset.voice === current;
      card.classList.toggle("chosen", chosen);
      card.setAttribute("aria-checked", String(chosen));
    }
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
    const talkRows = [row("Conversation mode",
      v.talk_enabled ? "On: “Hey Bridge” and ⌘⇧Space start a live conversation you can interrupt. Your voice goes to OpenAI while you talk."
        : "Talk back and forth with Bridge and interrupt it anytime. Your voice goes to OpenAI while a conversation is open.",
      toggle("Conversation mode", v.talk_enabled, (on) => save({talk_enabled: on})))];
    if (v.talk_enabled) {
      talkRows.push(
        voiceGallery(v.talk_voice),
        row("Model", v.talk_model === "gpt-realtime-mini" ? "Faster and cheaper; a little less capable." : "The most capable voice model.",
          segmented("Model", [["gpt-realtime-2.1", "Best"], ["gpt-realtime-mini", "Faster & cheaper"]], v.talk_model, (key) => save({talk_model: key}))),
      );
      const usage = row("This month", "Loading…", null);
      talkRows.push(usage);
      ui().request("/api/v1/talk").then((data) => {
        usage.querySelector(".set-hint").textContent = `${data.usage.minutes} minutes of conversation. Realtime voice costs more per minute than typing.`;
      }).catch(() => {});
    }
    const keep = node("select", undefined, "small-select");
    keep.setAttribute("aria-label", "Keep meeting notes for");
    for (const days of [7, 30, 90]) {
      const option = node("option", `${days} days`);
      option.value = String(days);
      option.selected = days === v.meeting_keep_days;
      keep.append(option);
    }
    keep.addEventListener("change", () => save({meeting_keep_days: Number(keep.value)}));
    const meetingRows = [row("Meeting notes",
      v.meetings_enabled ? "On: “take notes” or the button on Today. Your mic and the call's sound are transcribed on this Mac (or by OpenAI if speech to text is set to OpenAI); audio is never saved; the words go to OpenAI for the summary."
        : "Bridge can take notes in your meetings: live notes, then a summary, decisions, action items and a follow-up email.",
      toggle("Meeting notes", v.meetings_enabled, (on) => save({meetings_enabled: on})))];
    if (v.meetings_enabled) meetingRows.push(row("Keep meeting notes for", "Then they're deleted. You can delete any sooner.", keep));
    $("settings-voice").replaceChildren(
      ...talkRows,
      ...meetingRows,
      row("Speech to text", v.speech === "local" ? "On this Mac with Whisper. Your voice never leaves the Mac." : "OpenAI transcribes your recordings.",
        segmented("Speech to text", [["local", "On this Mac"], ["openai", "OpenAI"]], v.speech, (key) => save({speech: key}))),
      row("Tidy up dictation", "Removes “um”s and applies spoken corrections. Sends the text, never audio, to OpenAI.",
        toggle("Tidy up dictation", v.dictation_cleanup, (on) => save({dictation_cleanup: on}))),
      row("Stop dictating after a pause of", "", pause),
      wakeRow(),
      trainRow(),
    );
  }

  // "Hey Bridge" lives in the app (the microphone), which tells the page its state.
  function wakeRow() {
    const voice = window.BridgeNative && window.BridgeNative.state;
    if (!voice) return row("Listen for “Hey Bridge”", "Available in the Bridge app on your Mac.", null);
    const hint = !voice.ready ? "Train it with your voice first (below)."
      : voice.listening ? "On: Bridge listens for its name on this Mac. Audio is never sent anywhere to listen."
      : "Off. Use the shortcut or the panel's mic instead.";
    const control = toggle("Listen for Hey Bridge", voice.listening, (on) => window.BridgeNative.send({action: on ? "wake_on" : "wake_off"}));
    control.disabled = !voice.ready && !voice.listening;
    return row("Listen for “Hey Bridge”", hint, control);
  }

  function trainRow() {
    const voice = window.BridgeNative && window.BridgeNative.state;
    if (!voice) return row("Your voice for “Hey Bridge”", "Available in the Bridge app on your Mac.", null);
    // Training speaks through the voice status: "Say “Hey Bridge”…", "Learning your voice… 40%".
    const training = ["recording", "processing", "starting"].includes(voice.state) && !voice.listening
      && /hey bridge|say|quiet|learning your voice|louder|last one/i.test(voice.message || "");
    const hint = training ? voice.message : voice.ready ? "Trained on your voice. Retrain if it misses you or wakes by mistake."
      : "Record yourself saying “Hey Bridge” a few times so it recognises you.";
    const button = node("button", voice.ready ? "Retrain" : "Train with my voice", "secondary small");
    button.type = "button";
    button.disabled = training;
    button.addEventListener("click", () => window.BridgeNative.send({action: "train_wake"}));
    return row("Your voice for “Hey Bridge”", hint, button);
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
      ...(data.voice.talk_enabled ? [row("Conversations", "Conversation mode is on: while you talk with Bridge, your voice streams to OpenAI.", goes())] : []),
      row("Sign-ins, history and files", "Account tokens live in your Mac's Keychain.", stays()),
      keepChatsRow(p.keep_chats_days),
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

  function keepChatsRow(days) {
    const end = node("span", undefined, "set-end");
    end.append(segmented("Keep chats", [[0, "Off"], [7, "7 days"], [30, "30 days"]], days, (key) => {
      if (key === 0 && !window.confirm("Stop keeping chats? Your saved chats are deleted now.")) return;
      save({keep_chats_days: key});
    }));
    const clear = node("button", "Delete all", "secondary small");
    clear.type = "button";
    clear.addEventListener("click", async () => {
      if (!window.confirm("Delete all saved chats? This can't be undone.")) return;
      try {
        const {deleted} = await ui().request("/api/v1/chats/delete", "POST", {all: true});
        ui().notify(deleted ? `Deleted ${deleted} ${deleted === 1 ? "chat" : "chats"}.` : "There were no saved chats.");
      } catch (error) {
        ui().notify(error.message, true);
      }
    });
    if (days) end.append(clear);
    const hint = days ? `Recent chats in Ask stay on this Mac for ${days} days, then they're deleted.`
      : "Off: each chat is forgotten when Bridge quits.";
    return row("Keep chats", hint, end);
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

  // Health: every part of Bridge, whether it works, and the one tap that fixes it.
  const STATES = {ok: ["Working", "good"], warn: ["Check", "warm"], error: ["Broken", "bad"], off: ["Off", ""]};

  function fixButton(fix) {
    if (!fix) return null;
    if (fix.url) {
      const open = node("a", fix.label, "secondary small button-link");
      open.href = fix.url;
      return open;
    }
    const press = node("button", fix.label, "secondary small");
    press.type = "button";
    press.addEventListener("click", async () => {
      if (fix.view) {
        ui().view(fix.view, fix.section);
        return;
      }
      press.disabled = true;
      try {
        await ui().request(fix.action, "POST");
        ui().notify("Started. Check again in a moment.");
      } catch (error) {
        ui().notify(error.message, true);
      }
      setTimeout(renderHealth, 2500);
    });
    return press;
  }

  async function renderHealth() {
    let data;
    try {
      data = await ui().request("/api/v1/health");
    } catch (error) {
      $("health-summary").textContent = error.message;
      return;
    }
    healthDot(data);
    const problems = data.errors + data.warnings;
    $("health-summary").textContent = !problems ? "Everything is working."
      : data.errors ? `${data.errors} ${data.errors === 1 ? "thing is" : "things are"} broken${data.warnings ? `, and ${data.warnings} to check` : ""}. Each has a fix below.`
      : `${data.warnings} ${data.warnings === 1 ? "thing" : "things"} to check below.`;
    renderWeek(data.week);
    const box = $("settings-health");
    box.replaceChildren();
    const areas = [...new Set(data.checks.map((c) => c.area))];
    for (const area of areas) {
      const card = node("div", undefined, "card group health-group");
      card.append(node("p", area, "label health-area"));
      const items = data.checks.filter((c) => c.area === area)
        .sort((a, b) => ["error", "warn", "ok", "off"].indexOf(a.state) - ["error", "warn", "ok", "off"].indexOf(b.state));
      for (const check of items) {
        const [text, tone] = STATES[check.state] || STATES.off;
        const end = node("span", undefined, "set-end");
        end.append(pill(text, tone));
        const fix = fixButton(check.fix);
        if (fix && check.state !== "ok") end.append(fix);
        card.append(row(check.name, check.detail, end));
      }
      box.append(card);
    }
  }

  // What AI cost this week, next to the week before, and what it went on.
  function renderWeek(week) {
    const card = $("health-week");
    card.hidden = !week;
    if (!week) return;
    const money = (n) => n < 0.005 ? "$0" : `$${n.toFixed(2)}`;
    card.replaceChildren();
    const head = node("div", undefined, "hw-head");
    const words = node("div", undefined, "grow");
    words.append(node("p", "AI this week", "label"), node("p", money(week.spent), "hw-amount"));
    const change = node("p", undefined, "hw-change");
    if (week.previous) {
      const diff = week.spent - week.previous;
      change.textContent = Math.abs(diff) < 0.01 ? "Same as the week before"
        : `${diff > 0 ? "▲" : "▼"} ${money(Math.abs(diff))} vs the week before`;
      change.classList.add(diff > 0.01 ? "up" : "down");
    } else {
      change.textContent = `${week.calls} AI ${week.calls === 1 ? "call" : "calls"} in the last 7 days`;
    }
    words.append(change);
    const open = node("button", "Details", "secondary small");
    open.type = "button";
    open.addEventListener("click", () => show("usage"));
    head.append(words, open);
    card.append(head);
    if (week.top.length) {
      const most = Math.max(...week.top.map((t) => t.cost));
      const list = node("div", undefined, "hw-tasks");
      for (const t of week.top) {
        const line = node("div", undefined, "hw-task");
        const bar = node("span", undefined, "hw-bar");
        bar.style.setProperty("--w", `${Math.max(6, Math.round((t.cost / most) * 100))}%`);
        line.append(node("span", t.label, "grow"), bar, node("span", money(t.cost), "hw-cost"));
        list.append(line);
      }
      card.append(list);
    }
    if (week.budget) {
      const share = Math.min(1, week.month / week.budget);
      const meter = node("div", undefined, "hw-meter" + (share >= 1 ? " over" : share >= 0.8 ? " near" : ""));
      const fill = node("span");
      fill.style.width = `${Math.round(share * 100)}%`;
      meter.append(fill);
      card.append(meter, node("p", `${money(week.month)} of this month's ${money(week.budget)} budget`, "hint"));
    }
  }

  // Updates -----------------------------------------------------------------------------------

  let updatePoll = null;

  async function renderUpdates(action) {
    const card = $("updates-card");
    if (!card) return;
    let data;
    try {
      data = action ? await ui().request(`/api/v1/updates/${action}`, "POST")
        : await ui().request("/api/v1/updates");
    } catch (error) {
      if (!action) { card.hidden = true; return; }
      data = await ui().request("/api/v1/updates").catch(() => null);
      if (!data) return;
      data.error = error.message;
    }
    card.hidden = false;
    card.replaceChildren();
    const head = node("div", undefined, "upd-head");
    const words = node("div", undefined, "set-words");
    words.append(node("span", `Bridge ${data.current}`, "set-name"));
    const hint = node("span", undefined, "set-hint");
    const end = node("span", undefined, "set-end");
    const button = (label, next, primary) => {
      const b = node("button", label, primary ? "primary small" : "secondary small");
      b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        await renderUpdates(next);
      });
      return b;
    };
    const s = data.state;
    if (!data.supported) {
      hint.textContent = "This copy runs from its code, so it updates with git, not here.";
    } else if (s === "checking") {
      hint.textContent = "Checking for a new version…";
    } else if (s === "downloading") {
      hint.textContent = `Getting Bridge ${data.latest}… ${Math.round((data.progress || 0) * 100)}%`;
      const meter = node("div", undefined, "hw-meter");
      const fill = node("span");
      fill.style.width = `${Math.round((data.progress || 0) * 100)}%`;
      meter.append(fill);
      words.append(meter);
    } else if (s === "ready") {
      hint.textContent = `Bridge ${data.latest} is ready. Bridge closes for a moment and opens the new version.`;
      end.append(button("Restart to update", "install", true));
    } else if (s === "available") {
      hint.textContent = `Bridge ${data.latest} is out.`;
      end.append(button("Download", "download", true));
    } else if (s === "installing") {
      hint.textContent = "Restarting with the new version…";
    } else if (s === "current") {
      hint.textContent = "You have the latest version.";
      end.append(button("Check again", "check"));
    } else {
      hint.textContent = data.checked_at ? "No new version yet." : "Bridge looks for a new version once a day.";
      end.append(button("Check for updates", "check"));
    }
    words.append(hint);
    if (data.error) words.append(node("span", data.error, "set-hint upd-error"));
    if (data.notes && ["available", "ready", "downloading"].includes(s)) {
      words.append(node("span", data.notes, "set-hint upd-notes"));
    }
    head.append(words, end);
    card.append(head);
    if (data.supported) {
      const auto = toggle("Get new versions automatically", data.automatic, async (on) => {
        try { await ui().request("/api/v1/settings/app", "POST", {updates_automatic: on}); } catch { /* shown next time */ }
        renderUpdates();
      });
      card.append(row("Check every day", "Bridge downloads a new version in the background and asks before restarting. Only versions signed by Bridge's developer and approved by Apple are installed.", auto));
    }
    clearTimeout(updatePoll);
    if (["checking", "downloading"].includes(s) && section === "about") updatePoll = setTimeout(() => renderUpdates(), 1000);
  }

  function healthDot(data) {
    const dot = document.getElementById("health-dot");
    if (dot) dot.hidden = !data.errors;
  }

  async function checkHealthQuietly() {
    try { healthDot(await ui().request("/api/v1/health")); } catch { /* not now */ }
  }

  async function renderOperator() {
    const box = $("settings-operator");
    if (!box || !data.operator) return;
    const o = data.operator;
    const perms = (data.permissions || []).filter((p) => /accessibility|screen/i.test(p.name));
    const rows = [
      row("Let Bridge use your Mac",
        o.enabled ? "On. Bridge can do tasks in your apps, or “show me how” — highlight each step for you to do yourself. Screenshots go to OpenAI only while one is running; Stop ends it."
          : "Off. When on, Bridge can click and type in your apps for you (asking before anything important), or show you how, step by step, with a highlight on screen.",
        toggle("Let Bridge use your Mac", o.enabled, (on) => save({operator_enabled: on}))),
    ];
    if (o.enabled) {
      rows.push(row("Ask me before", o.ask === "every" ? "Every click and keystroke waits for your Go ahead." : "Sending, deleting, buying, submitting, Return in chat apps, ⌘Q — the steps with consequences.",
        segmented("Ask me before", [["important", "Important steps"], ["every", "Every step"]], o.ask, (key) => save({operator_ask: key}))));
      for (const p of perms) {
        const end = node("span", undefined, "set-end");
        end.append(pill(p.state === "allowed" ? "Allowed" : "Needed", p.state === "allowed" ? "good" : "warm"));
        if (p.state !== "allowed") {
          const open = node("a", "Open Settings", "secondary small button-link");
          open.href = p.settings_url;
          end.append(open);
        }
        rows.push(row(p.name, p.state === "allowed" ? "" : "Bridge needs this to see the screen and to click and type.", end));
      }
      rows.push(row("Never", "Password managers, Keychain, System Settings, Bridge itself, and apps you block in Privacy. Bridge never types into password fields.", null));
    }
    box.replaceChildren(...rows);
    const recent = $("settings-operator-recent");
    recent.hidden = !o.enabled;
    if (!o.enabled) return;
    try {
      const info = await ui().request("/api/v1/operator");
      recent.replaceChildren();
      recent.hidden = !info.recent.length;
      for (const item of info.recent.slice(0, 6)) {
        const when = new Date(item.started * 1000).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
        recent.append(row(item.task, `${when} · ${item.result || item.status} · ${item.actions} steps`,
          pill(item.status === "done" ? "Done" : item.status === "stopped" ? "Stopped" : "Didn't finish", item.status === "done" ? "good" : "")));
      }
    } catch { recent.hidden = true; }
  }

  function render() {
    if (!data) return;
    $("restart-banner").hidden = !data.restart_needed;
    $("restart-now").hidden = !data.can_restart;
    renderShortcuts();
    paintShot();
    renderVoice();
    renderPrivacy();
    renderPermissions();
    renderOperator();
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

  document.addEventListener("bridge-voice", () => { if (data && section === "voice") renderVoice(); });
  $("health-recheck").addEventListener("click", renderHealth);
  // Panels in screenshots, for 10 minutes.
  function paintShot() {
    const until = (data && data.screenshot_until) || 0;
    const on = until * 1000 > Date.now();
    $("shot-toggle").textContent = on ? "Turn off now" : "Turn on for 10 min";
    $("shot-hint").textContent = on
      ? `On until ${new Date(until * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}: Bridge's panels show in screenshots and screen sharing.`
      : "Bridge's floating panels are left out of screenshots and screen sharing. Turn this on for 10 minutes to take a screenshot of one.";
  }
  $("shot-toggle").addEventListener("click", async () => {
    const on = !(((data && data.screenshot_until) || 0) * 1000 > Date.now());
    try {
      const result = await ui().request("/api/v1/app/screenshot-mode", "POST", {on});
      if (data) data.screenshot_until = result.until;
      paintShot();
      ui().notify(on ? "Panels show in screenshots for 10 minutes." : "Panels are private again.");
    } catch (error) {
      ui().notify(error.message, true);
    }
  });

  window.BridgeSettings = {show, refresh, current: () => section, checkHealth: checkHealthQuietly};
})();
