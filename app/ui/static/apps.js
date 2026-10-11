"use strict";

// Connections › More apps: MCP apps (Notion, Linear, Sentry…). Add one from the list or by
// address/program, sign in, choose per tool whether Bridge asks first, share results or not.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  const {node, button} = window.BridgeDOM;
  let kind = "http";
  let open = null;  // The app whose tools are showing.

  const STATE = {ok: ["Connected", "good"], sign_in: ["Sign in", "warm"], error: ["Problem", "bad"], unknown: ["…", ""]};
  const MODES = [["auto", "As the app says"], ["ask", "Always ask"], ["off", "Off"]];

  async function act(task, message) {
    try {
      const result = await task();
      if (message) ui().notify(message);
      return result;
    } catch (error) {
      ui().notify(error.message, true);
      return null;
    } finally {
      refresh();
    }
  }

  async function signIn(server) {
    const result = await act(() => ui().request("/api/v1/mcp/signin", "POST", {id: server.id}));
    if (result) {
      window.open(result.url, "_blank", "noopener");
      ui().notify(`Finish signing in to ${server.name} in your browser, then come back.`);
      // Watch for the sign-in to land.
      let tries = 0;
      const timer = setInterval(async () => {
        tries += 1;
        const data = await ui().request("/api/v1/mcp").catch(() => null);
        const now = data && data.servers.find((s) => s.id === server.id);
        if ((now && now.state === "ok") || tries > 60) {
          clearInterval(timer);
          render(data);
        }
      }, 3000);
    }
  }

  function serverRow(server) {
    const box = node("div", undefined, "mcp-server");
    const [label, tone] = STATE[server.state] || STATE.unknown;
    const top = node("div", undefined, "set");
    const words = node("div", undefined, "set-words");
    words.append(node("span", server.name, "set-name"),
      node("span", server.state === "ok" ? `${server.tools} tools · ${server.url || server.command}` : server.detail, "set-hint"));
    const end = node("span", undefined, "set-end");
    end.append(node("span", label, `pill-state ${tone}`));
    if (server.state === "sign_in") end.append(button("Sign in", "primary small", () => signIn(server)));
    if (server.state === "error") end.append(button("Try again", "secondary small", () => act(() => ui().request("/api/v1/mcp/connect", "POST", {id: server.id}))));
    if (server.state === "ok") end.append(button(open === server.id ? "Hide tools" : "Tools", "secondary small", () => { open = open === server.id ? null : server.id; refresh(); }));
    top.append(words, end);
    box.append(top);
    if (open === server.id) box.append(toolsPanel(server));
    return box;
  }

  function toolsPanel(server) {
    const panel = node("div", undefined, "mcp-tools");
    const share = node("label", undefined, "allow");
    const check = node("input");
    check.type = "checkbox";
    check.checked = server.shared;
    check.addEventListener("change", () => act(() => ui().request("/api/v1/mcp/share", "POST", {id: server.id, shared: check.checked})));
    share.append(check, node("span", "Let the model read this app's results (so Bridge can answer about them)"));
    panel.append(share, node("p", "Loading tools…", "hint"));
    ui().request(`/api/v1/mcp/tools?id=${encodeURIComponent(server.id)}`).then((data) => {
      panel.lastChild.remove();
      for (const tool of data.tools) {
        const row = node("div", undefined, "set mcp-tool");
        const words = node("div", undefined, "set-words");
        words.append(node("span", `${tool.title}${tool.read_only ? "" : " · changes things"}`, "set-name"), node("span", tool.description, "set-hint"));
        const select = node("select", undefined, "small-select");
        select.setAttribute("aria-label", `When Bridge uses ${tool.title}`);
        for (const [value, text] of MODES) {
          const option = node("option", value === "auto" ? (tool.read_only ? "Don't ask" : "Ask first") : text);
          option.value = value;
          option.selected = value === tool.mode;
          select.append(option);
        }
        select.addEventListener("change", () => act(() => ui().request("/api/v1/mcp/pref", "POST", {id: server.id, tool: tool.name, mode: select.value})));
        row.append(words, select);
        panel.append(row);
      }
      panel.append(button(`Remove ${server.name}`, "ghost danger small", () => {
        if (window.confirm(`Remove ${server.name} from Bridge? Its sign-in is deleted from this Mac.`)) {
          act(() => ui().request("/api/v1/mcp/remove", "POST", {id: server.id}), `${server.name} removed.`);
        }
      }));
    }).catch((error) => { panel.lastChild.textContent = error.message; });
    return panel;
  }

  function render(data) {
    if (!data) return;
    $("mcp-servers").replaceChildren(...data.servers.map(serverRow));
    if (!data.servers.length) $("mcp-servers").append(node("p", "No apps yet. Pick one below.", "hint"));
    const presets = $("mcp-presets");
    presets.replaceChildren();
    for (const preset of data.presets) {
      const tile = button("", `mcp-preset${preset.added ? " added" : ""}`, () => {
        if (preset.added) return;
        act(async () => {
          const added = await ui().request("/api/v1/mcp/add", "POST", {name: preset.name, url: preset.url, kind: "http"});
          if (added.state === "sign_in") signIn(added);
          return added;
        }, preset.added ? null : `${preset.name} added.`);
      });
      tile.append(node("strong", preset.name), node("span", preset.added ? "Added" : preset.what, "faint"));
      tile.disabled = preset.added;
      presets.append(tile);
    }
  }

  async function refresh() {
    try { render(await ui().request("/api/v1/mcp")); } catch { /* Connections shows its own errors */ }
  }

  document.querySelectorAll("[data-mcp-kind]").forEach((choice) => choice.addEventListener("click", () => {
    kind = choice.dataset.mcpKind;
    document.querySelectorAll("[data-mcp-kind]").forEach((c) => {
      c.classList.toggle("on", c === choice);
      c.setAttribute("aria-checked", String(c === choice));
    });
    document.querySelectorAll(".mcp-web").forEach((el) => { el.hidden = kind !== "http"; });
    document.querySelectorAll(".mcp-local").forEach((el) => { el.hidden = kind !== "stdio"; });
  }));

  $("mcp-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const name = $("mcp-name").value.trim();
    const body = {name: name || "App", kind};
    if (kind === "http") {
      body.url = $("mcp-url").value.trim();
      const key = $("mcp-key").value.trim();
      if (key) body.headers = {Authorization: key.toLowerCase().startsWith("bearer ") ? key : `Bearer ${key}`};
    } else {
      const parts = $("mcp-command").value.trim().split(/\s+/).filter(Boolean);
      body.command = parts[0] || "";
      body.args = parts.slice(1);
    }
    act(async () => {
      const added = await ui().request("/api/v1/mcp/add", "POST", body);
      $("mcp-key").value = "";
      if (added.state === "sign_in") signIn(added);
      return added;
    }, "App added.");
  });

  window.BridgeApps = {refresh};
})();
