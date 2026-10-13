"use strict";

// ⌃⌥Space: act on the text you selected (or the whole window). Improve, shorten, fix,
// translate, summarize, explain, draft a reply, or anything you type; then Replace pastes it
// back into the app you were in, Copy copies it, Ask Bridge hands it over.
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  const ACTIONS = [
    ["improve", "Improve"], ["shorter", "Shorter"], ["grammar", "Fix grammar"], ["translate", "Translate"],
    ["summarize", "Summarize"], ["explain", "Explain"], ["reply", "Draft reply"], ["remind", "Remind me"],
  ];
  let state = {mode: "selection", app: "", allowed: true, page: {}};
  let busy = false;

  function status(text, tone = "") {
    $("tx-status").textContent = text;
    $("tx-status").className = `tx-status ${tone}`;
    fit();
  }

  function fit() {
    requestAnimationFrame(() => {
      const main = $("tx");
      const style = getComputedStyle(main);
      const shown = [...main.children].filter((c) => !c.hidden && c.tagName !== "LABEL");
      const height = shown.reduce((sum, c) => sum + c.getBoundingClientRect().height, 0)
        + (shown.length - 1) * parseFloat(style.rowGap || "0")
        + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      mini.native({action: "text_size", height: Math.ceil(height + 2)});
    });
  }

  function setBusy(on) {
    busy = on;
    for (const b of $("tx-actions").children) b.disabled = on;
    $("tx").classList.toggle("busy", on);
  }

  function showResult(text) {
    $("tx-result").hidden = false;
    $("tx-output").value = text;
    fit();
  }

  async function run(action) {
    const text = $("tx-input").value.trim();
    if (busy) return;
    if (!text) return status("There's no text yet. Type or paste some above.", "warn");
    if (action === "remind") {
      const where = state.page.url || state.page.file || "";
      const request = state.mode === "window"
        ? `Remind me about “${state.page.window || "this window"}” ${where}`.trim()
        : `Create a reminder from this text:\n\n${text}`;
      return mini.native({action: "text_ask", request});
    }
    const instruction = $("tx-instruction").value.trim();
    if (action === "custom" && !instruction) return status("Type what you'd like done, then press Return.", "warn");
    setBusy(true);
    $("tx-result").hidden = false;
    $("tx-output").value = "";
    $("tx-output").classList.add("loading");
    status("Working on it…");
    try {
      const data = await mini.request("/api/v1/text/transform", "POST", {action, text, instruction});
      $("tx-output").classList.remove("loading");
      showResult(data.result);
      status("Done. Replace it, copy it, or edit it first.", "good");
      $("tx-replace").disabled = state.mode !== "selection";
      $("tx-output").focus();
    } catch (error) {
      $("tx-output").classList.remove("loading");
      $("tx-result").hidden = true;
      status(error.message, "warn");
    } finally {
      setBusy(false);
    }
  }

  function show(payload) {
    state = {mode: payload.mode, app: payload.app || state.app, allowed: payload.allowed !== false, page: payload.page || {}};
    $("tx-input").value = payload.text || "";
    $("tx-instruction").value = "";
    $("tx-result").hidden = true;
    $("tx-output").value = "";
    $("tx-source").textContent = payload.mode === "window"
      ? (payload.title ? `Window: ${payload.title.slice(0, 50)}` : `Whole window${state.app ? ` in ${state.app}` : ""}`)
      : (state.app ? `Selected in ${state.app}` : "Selected text");
    $("tx-ask").textContent = state.allowed ? "Ask Bridge…" : "Allow access…";
    if (!state.allowed) status("Turn on Bridge in Accessibility settings, then press ⌃⌥Space again — or paste text above.", "warn");
    else if (payload.reading) status(`Nothing selected — reading the ${state.app || "front"} window…`);
    else if (!(payload.text || "").trim()) status("Nothing was selected. Type or paste text above.", "warn");
    else status(payload.mode === "window" ? "Choose an action for this window, or type what to do." : "Choose an action, or type what to do.");
    setBusy(Boolean(payload.reading));
    setTimeout(() => ((payload.text || "").trim() ? $("tx-instruction") : $("tx-input")).focus(), 30);
    fit();
  }

  function failed(message) {
    setBusy(false);
    status(message, "warn");
    $("tx-input").focus();
  }

  for (const [key, label] of ACTIONS) {
    $("tx-actions").append(button(label, "tx-chip", () => run(key)));
  }
  $("tx-custom").addEventListener("submit", (event) => { event.preventDefault(); run("custom"); });
  $("tx-replace").addEventListener("click", () => mini.native({action: "text_replace", text: $("tx-output").value}));
  $("tx-copy").addEventListener("click", () => { mini.native({action: "text_copy", text: $("tx-output").value}); status("Copied.", "good"); });
  $("tx-ask").addEventListener("click", () => {
    if (!state.allowed) return mini.native({action: "text_allow"});
    const instruction = $("tx-instruction").value.trim() || "Help me with this text";
    const request = state.mode === "window" ? `${instruction} (about the window I'm looking at)` : `${instruction}:\n\n${$("tx-input").value.trim()}`;
    mini.native({action: "text_ask", request});
  });
  $("tx-close").addEventListener("click", () => mini.native({action: "hide"}));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") mini.native({action: "hide"});
    if (event.key === "Enter" && event.metaKey && !$("tx-result").hidden && !$("tx-replace").disabled) $("tx-replace").click();
  });

  window.BridgeText = {show, failed};
  mini.ready.then(() => mini.native({action: "text_ready"}));
})();
