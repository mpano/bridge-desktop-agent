"use strict";

// The panel at the bottom of the screen while Bridge uses your Mac: each step as it happens,
// Go ahead / Stop when a step needs your OK, Carry on after you took over the mouse.
(() => {
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  let run = null;
  let closing = null;

  const DONE = {done: "Done", failed: "Stopped", stopped: "Stopped"};

  function render() {
    if (!run) return;
    const finished = run.status in DONE;
    $("op").dataset.state = run.status;
    $("op-title").textContent = finished ? DONE[run.status] : run.status === "paused" ? "Paused" : "Bridge is using your Mac";
    $("op-task").textContent = run.task;
    $("op-step").textContent = finished ? run.result : run.step;
    // The last couple of finished steps, faded, so you can follow along.
    const done = $("op-done");
    done.replaceChildren();
    if (!finished) {
      for (const step of (run.steps || []).slice(-2)) {
        const item = document.createElement("li");
        item.textContent = step;
        done.append(item);
      }
    }
    $("op-title").dataset.count = run.actions ? `Step ${run.actions}` : "";
    $("op-stop").hidden = finished;
    const asking = Boolean(run.approval);
    $("op-ask").hidden = !asking;
    if (asking) {
      $("op-ask-text").textContent = run.approval.text;
      $("op-yes").textContent = run.approval.kind === "pause" ? "Carry on" : "Go ahead";
    }
    if (finished && !closing) closing = setTimeout(() => mini.native({action: "hide"}), 6000);
    if (!finished && closing) { clearTimeout(closing); closing = null; }
  }

  async function poll() {
    try {
      const data = await mini.request("/api/v1/operator/current");
      run = data.current;
      render();
    } catch { /* Bridge restarting: try again */ }
    setTimeout(poll, run && !(run.status in DONE) ? 400 : 1500);
  }

  async function send(path, body) {
    if (!run) return;
    try { run = await mini.request(path, "POST", {run_id: run.id, ...body}); render(); } catch { /* next poll */ }
  }

  $("op-yes").addEventListener("click", () => send("/api/v1/operator/decide", {ok: true}));
  $("op-no").addEventListener("click", () => send("/api/v1/operator/stop", {}));
  $("op-stop").addEventListener("click", () => send("/api/v1/operator/stop", {}));
  mini.ready.then(poll);
})();
