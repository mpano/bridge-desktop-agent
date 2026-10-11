"use strict";

// "What's this?" (⌃⌥W): the card next to the pointer. The name of what's under the pointer
// shows at once (from macOS), then Bridge's explanation; ask a follow-up, have Bridge show
// you how to use it, or copy the explanation. Esc or a click anywhere else closes it.
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  let spot = null;
  let run = 0;  // Ignore answers for a spot you've moved on from.

  function state(name) {
    $("ex").dataset.state = name;
  }

  // The card grows to fit what it says (the app keeps it next to the pointer).
  function fit() {
    requestAnimationFrame(() => {
      const main = $("ex");
      const style = getComputedStyle(main);
      const shown = [...main.children].filter((child) => !child.hidden);
      const needed = shown.reduce((sum, child) => sum + child.scrollHeight, 0)
        + (shown.length - 1) * parseFloat(style.rowGap || "0")
        + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      mini.native({action: "explain_size", height: Math.ceil(needed + 2)});
    });
  }

  function loading() {
    const box = node("div", undefined, "ex-loading");
    box.append(node("span"), node("span"), node("span"));
    return box;
  }

  function fail(message) {
    state("failed");
    $("ex-body").replaceChildren(node("p", message, "ex-error"));
    $("ex-actions").replaceChildren(button("Close", "ghost small", close));
    fit();
  }

  function close() {
    mini.native({action: "hide"});
  }

  function render() {
    $("ex-kind").textContent = spot.kind || "on screen";
    $("ex-name").textContent = spot.title || spot.name || "This";
    $("ex-app").textContent = spot.app ? `in ${spot.app}` : "";
    const body = $("ex-body");
    body.replaceChildren();
    if (!spot.ready) {
      body.append(loading());
      fit();
      return;
    }
    if (spot.what) body.append(node("p", spot.what, "ex-what"));
    if (spot.use) body.append(node("p", spot.use, "ex-use"));
    const tip = spot.tip || (spot.shortcut ? `Shortcut: ${spot.shortcut}` : "");
    if (tip) body.append(node("p", tip, "ex-tip"));
    const actions = $("ex-actions");
    actions.replaceChildren(
      button("Show me how to use it", "secondary small", showMe),
      button("Ask about it", "ghost small", () => {
        $("ex-ask").hidden = false;
        $("ex-question").focus();
        fit();
      }),
      button("Copy", "ghost small", async () => {
        try { await navigator.clipboard.writeText([spot.title || spot.name, spot.what, spot.use, tip].filter(Boolean).join("\n")); } catch { /* not allowed */ }
      }),
    );
    fit();
  }

  async function start(x, y) {
    const mine = ++run;
    spot = null;
    state("looking");
    $("ex-ask").hidden = true;
    $("ex-question").value = "";
    $("ex-kind").textContent = "";
    $("ex-name").textContent = "Looking…";
    $("ex-app").textContent = "";
    $("ex-body").replaceChildren(loading());
    $("ex-actions").replaceChildren();
    try {
      const found = await mini.request("/api/v1/explain/look", "POST", {x, y});
      if (mine !== run) return;
      spot = found;
      mini.native({action: "highlight", frame: found.frame, point: found.point});
      render();
      spot = await mini.request("/api/v1/explain", "POST", {id: found.id});
      if (mine !== run) return;
      state("ready");
      render();
    } catch (error) {
      if (mine === run) fail(error.message);
    }
  }

  async function showMe() {
    try {
      await mini.request("/api/v1/explain/show-me", "POST", {id: spot.id});
      close();
    } catch (error) {
      $("ex-body").append(node("p", error.message, "ex-error"));
    }
  }

  $("ex-ask").addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = $("ex-question").value.trim();
    if (!question || !spot) return;
    $("ex-question").value = "";
    const asked = node("p", question, "ex-q");
    const answer = node("div", undefined, "ex-a");
    answer.append(loading());
    $("ex-body").append(asked, answer);
    answer.scrollIntoView({block: "end"});
    try {
      const reply = await mini.request("/api/v1/explain/ask", "POST", {id: spot.id, question});
      answer.replaceChildren(node("p", reply.answer));
    } catch (error) {
      answer.replaceChildren(node("p", error.message, "ex-error"));
    }
    answer.scrollIntoView({block: "end"});
    fit();
  });
  $("ex-close").addEventListener("click", close);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });

  window.BridgeExplain = {start: (x, y) => mini.ready.then(() => start(x, y))};
})();
