"use strict";

// The Show me how panel: the current step in big words, Next and "Do this step for me";
// Bridge's questions with a box to answer; and at the end, the steps and "Save as a how-to".
// The highlight itself is drawn on screen by the app; this page polls what the guide is doing.
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  const FINISHED = new Set(["done", "failed", "stopped"]);
  let guide = null;
  let shownKey = "";
  let polling = null;
  let closing = null;
  let readAloud = false;
  try { readAloud = localStorage.getItem("bridge-guide-voice") === "1"; } catch { /* fine */ }

  // **bold** and `typed text` from the step, as safe text nodes.
  function rich(text) {
    const box = node("span");
    for (const part of String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
      if (!part) continue;
      if (part.startsWith("**")) box.append(node("strong", part.slice(2, -2)));
      else if (part.startsWith("`")) box.append(node("code", part.slice(1, -1), "gd-type"));
      else box.append(document.createTextNode(part));
    }
    return box;
  }

  async function send(what, extra = {}) {
    if (!guide) return;
    try {
      guide = await mini.request("/api/v1/guide/command", "POST", {id: guide.id, what, ...extra});
      render();
    } catch (error) {
      flash(error.message);
    }
  }

  function flash(text) {
    $("gd-body").append(node("p", text, "gd-error"));
  }

  async function speak(text) {
    if (!readAloud || !text) return;
    try {
      const response = await fetch("/api/v1/guide/speak", {method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({text})});
      if (!response.ok) return;
      const url = URL.createObjectURL(await response.blob());
      const audio = new Audio(url);
      audio.addEventListener("ended", () => URL.revokeObjectURL(url));
      audio.play().catch(() => {});
    } catch { /* reading aloud is optional */ }
  }

  function render() {
    if (!guide) return;
    const main = $("gd");
    main.dataset.state = guide.status;
    $("gd-task").textContent = guide.task;
    $("gd-title").textContent = guide.status === "done" ? "You did it" : guide.status === "failed" ? "Guide stopped" : "Show me how";
    $("gd-stop").hidden = FINISHED.has(guide.status);
    const body = $("gd-body");
    const actions = $("gd-actions");
    const key = `${guide.status}:${guide.number}:${guide.note}`;
    if (key === shownKey && !FINISHED.has(guide.status)) return;  // Keep the answer box as typed.
    shownKey = key;
    body.replaceChildren();
    actions.replaceChildren();

    if (guide.status === "waiting" && guide.step) {
      const step = guide.step;
      const line = node("p", undefined, "gd-step");
      line.append(node("span", `Step ${step.n}`, "gd-n"), rich(step.text));
      body.append(line);
      if (step.keys) body.append(node("p", step.keys, "gd-keys"));
      if (step.tip) body.append(node("p", step.tip, "gd-tip"));
      if (guide.note) body.append(node("p", guide.note, "gd-note faint"));
      if (step.typing) {
        actions.append(button("Copy text", "secondary small", async () => {
          try { await navigator.clipboard.writeText(step.typing); } catch { /* not allowed */ }
        }));
      }
      actions.append(
        button("Do this step for me", "secondary small", () => send("do")),
        button("Next ›", "primary small", () => send("next")),
        button("Do the rest for me", "link small gd-rest", () => send("rest")),
      );
      speak(step.text);
    } else if (guide.status === "asking") {
      body.append(node("p", guide.note, "gd-step"));
      const form = node("form", undefined, "gd-answer");
      const input = node("input");
      input.placeholder = "Your answer";
      input.maxLength = 500;
      input.setAttribute("aria-label", "Your answer");
      form.append(input, button("Send", "primary small", () => form.requestSubmit()));
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (input.value.trim()) send("answer", {answer: input.value.trim()});
      });
      body.append(form);
      actions.append(button("Do the rest for me", "link small gd-rest", () => send("rest")));
      setTimeout(() => input.focus(), 50);
      speak(guide.note);
    } else if (FINISHED.has(guide.status)) {
      body.append(node("p", guide.result, "gd-step"));
      if (guide.status === "done" && guide.steps.length) {
        const list = node("ol", undefined, "gd-list");
        for (const text of guide.steps.slice(-8)) {
          const item = node("li");
          item.append(rich(text));
          list.append(item);
        }
        body.append(list);
        actions.append(button(guide.saved ? "Saved to Notes ✓" : "Save as a how-to in Notes", guide.saved ? "ghost small" : "secondary small", async () => {
          if (guide.saved) return;
          try { guide = await mini.request("/api/v1/guide/save", "POST", {id: guide.id}); shownKey = ""; render(); } catch (error) { flash(error.message); }
        }));
      }
      actions.append(button("Close", "ghost small", () => mini.native({action: "hide"})));
      if (guide.status === "done") speak(guide.result);
    } else {
      body.append(node("p", guide.number ? "Nice. Looking at your screen for the next step…" : "Looking at your screen…", "gd-step faint"));
      actions.append(button("Stop", "ghost small", () => send("stop")));
    }
    if (FINISHED.has(guide.status) && guide.status !== "done" && !closing) closing = setTimeout(() => mini.native({action: "hide"}), 8000);
    if (!FINISHED.has(guide.status) && closing) { clearTimeout(closing); closing = null; }
  }

  async function poll() {
    clearTimeout(polling);
    try {
      const data = await mini.request("/api/v1/guide");
      const next = data.current;
      if (next && (!guide || next.id !== guide.id)) shownKey = "";
      guide = next;
      render();
    } catch { /* Bridge restarting: try again */ }
    polling = setTimeout(poll, guide && !FINISHED.has(guide.status) ? 350 : 1500);
  }

  function paintVoice() {
    $("gd-voice").textContent = readAloud ? "🔊" : "🔈";
    $("gd-voice").setAttribute("aria-pressed", String(readAloud));
  }
  $("gd-voice").addEventListener("click", () => {
    readAloud = !readAloud;
    try { localStorage.setItem("bridge-guide-voice", readAloud ? "1" : "0"); } catch { /* fine */ }
    paintVoice();
    if (readAloud && guide && guide.step) speak(guide.step.text);
  });
  $("gd-stop").addEventListener("click", () => send("stop"));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") send("stop");
  });
  paintVoice();
  window.BridgeGuide = {poll};
  mini.ready.then(poll);
})();
