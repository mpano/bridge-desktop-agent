"use strict";

// Shared by the menu bar panel and the command bar: sign-in with the launch ticket the app
// passes in, API calls, running a request and answering its approval card.
window.BridgeMini = (() => {
  const listeners = [];

  function native(message) {
    try { window.webkit.messageHandlers.bridge.postMessage(message); } catch { /* opened in a browser */ }
  }

  async function request(path, method = "GET", body) {
    const response = await fetch(path, {
      method, credentials: "same-origin", cache: "no-store",
      headers: body === undefined ? {} : {"Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Bridge said no (${response.status}).`);
    return data;
  }

  async function signIn() {
    const match = location.hash.match(/launch=([A-Za-z0-9_-]+)/);
    if (!match) return;
    history.replaceState(null, "", location.pathname);
    await request("/api/v1/session/launch", "POST", {ticket: match[1]}).catch(() => {});
  }

  async function follow(progress) {
    while (!progress.result) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      progress = await request(`/api/v1/tasks/${encodeURIComponent(progress.request_id)}`);
      if (progress.status === "interrupted") throw new Error("That stopped part-way. Check Ask in the Bridge window.");
    }
    return progress.result;
  }

  const run = async (message) => follow(await request("/api/v1/tasks", "POST", {message}));
  const decide = async (token, approved) => follow(await request("/api/v1/tasks/confirm", "POST", {token, approved}));

  function describe(item) {
    const a = item.arguments || {};
    const list = (value) => (Array.isArray(value) ? value.join(", ") : value || "");
    const quote = (text) => (text ? `“${String(text).replace(/\s+/g, " ").slice(0, 120)}${String(text).length > 120 ? "…" : ""}”` : "");
    switch (item.action) {
      case "email_send": return {title: `Email ${list(a.to)}`, detail: [a.subject, quote(a.body)].filter(Boolean).join(" — "), go: "Send"};
      case "slack_send_message": return {title: `Message ${a.channel}`, detail: quote(a.text), go: "Send"};
      case "messages_send": return {title: `Text ${a.to}`, detail: quote(a.text), go: "Send"};
      case "focus_start": return {title: `Focus for ${a.minutes} min`, detail: a.task || "Calendar · Slack · music", go: "Start"};
      default: return {title: item.message || "Allow this?", detail: "", go: "Approve"};
    }
  }

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

  // Called by the app each time the panel or bar is shown.
  function shown() { listeners.forEach((listener) => listener()); }
  const onShown = (listener) => listeners.push(listener);

  document.addEventListener("keydown", (event) => { if (event.key === "Escape") native({action: "hide"}); });

  return {native, request, run, decide, describe, node, button, shown, onShown, ready: signIn()};
})();
