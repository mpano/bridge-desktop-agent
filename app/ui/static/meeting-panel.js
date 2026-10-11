"use strict";

// The small panel in the top-right corner while Bridge takes meeting notes: recording dot,
// timer, the latest words, Pause / Stop / Open. Left out of screen sharing.
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  let closing = null;
  let lastDone = null;

  const clock = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

  async function send(path, body) {
    try { await mini.request(path, "POST", body || {}); } catch { /* next poll shows it */ }
    poll();
  }

  function render(data) {
    const current = data.current;
    const actions = $("mp-actions");
    actions.replaceChildren();
    const lines = $("mp-lines");
    lines.replaceChildren();
    if (current) {
      clearTimeout(closing);
      closing = null;
      $("mp").dataset.state = current.paused ? "paused" : "recording";
      $("mp-dot").className = `mt-rec${current.paused ? " paused" : ""}`;
      $("mp-title").textContent = current.paused ? "Notes paused" : "Taking notes";
      $("mp-sub").textContent = current.title;
      $("mp-clock").textContent = clock(current.minutes * 60);
      if (current.problem) lines.append(node("p", current.problem, "mt-warn"));
      for (const line of current.lines.slice(-3)) {
        const row = node("p", undefined, `mt-line ${line.who === "You" ? "you" : "them"}`);
        row.append(node("span", line.who, "mt-who"), document.createTextNode(line.text));
        lines.append(row);
      }
      if (!current.lines.length) lines.append(node("p", "Listening…", "faint"));
      actions.append(
        button(current.paused ? "Resume" : "Pause", "secondary small", () => send("/api/v1/meetings/pause", {paused: !current.paused})),
        button("Stop", "primary small", () => send("/api/v1/meetings/stop")),
        button("Open", "ghost small", () => mini.native({action: "open", view: "meetings"})),
      );
      return;
    }
    const writing = data.meetings.find((m) => m.status === "processing");
    const done = data.meetings[0];
    $("mp-dot").className = "mt-rec paused";
    $("mp-clock").textContent = "";
    if (writing) {
      $("mp").dataset.state = "processing";
      $("mp-title").textContent = "Writing it up…";
      $("mp-sub").textContent = writing.title;
      lines.append(node("p", "Summary, decisions and action items in a moment.", "faint"));
      return;
    }
    $("mp").dataset.state = "done";
    $("mp-title").textContent = done ? "Notes ready" : "Meeting notes";
    $("mp-sub").textContent = done ? done.title : "";
    if (done && done.summary) lines.append(node("p", done.summary, "mp-summary"));
    actions.append(button("Read the notes", "primary small", () => mini.native({action: "open", view: "meetings"})),
      button("Close", "ghost small", () => mini.native({action: "hide"})));
    if (done && done.id !== lastDone && !closing) {
      lastDone = done.id;
      closing = setTimeout(() => { closing = null; mini.native({action: "hide"}); }, 15000);
    }
  }

  async function poll() {
    let data = null;
    try { data = await mini.request("/api/v1/meetings"); render(data); } catch { /* restarting */ }
    const busy = data && (data.current || data.meetings.some((m) => m.status === "processing"));
    setTimeout(poll, busy ? 1500 : 5000);
  }

  window.BridgeMeetingPanel = {poll};
  mini.ready.then(poll);
})();
