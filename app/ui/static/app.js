"use strict";

// Sign-in uses an HttpOnly session cookie; approval tokens exist only in this closure.
(() => {
  const $ = (id) => document.getElementById(id);
  let signedIn = false;
  let setupToken = "";  // Only for first-run setup with the API token; never stored.
  let pending = null;
  let busy = false;
  let liveTask = null;
  let selectedWorkflow = null;
  let workflowOffset = 0;
  let workflowTotal = 0;
  let capabilities = [];
  const workflowLimit = 25;
  const dialog = $("confirmation-dialog");

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function notify(message, error = false) {
    $("notice").textContent = message;
    $("notice").className = error ? "error" : "";
    $("notice").hidden = !message;
  }

  function syncControls() {
    $("workspace").disabled = busy || !signedIn || Boolean(pending);
    $("disconnect").disabled = busy || Boolean(pending);
    $("approve").disabled = busy;
    $("decline").disabled = busy;
    $("review-workflows").disabled = busy;
    $("busy-status").hidden = !busy;
    $("workspace").setAttribute("aria-busy", String(busy));
  }

  async function request(path, method = "GET", body) {
    const headers = {};
    if (setupToken) headers.Authorization = `Bearer ${setupToken}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {method, headers, credentials: "same-origin", cache: "no-store",
        redirect: "error", body: body === undefined ? undefined : JSON.stringify(body)});
    } catch {
      throw new Error("Connection lost. If an action was submitted, review its workflow before retrying.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && signedIn && !path.startsWith("/api/v1/auth/")) {
        signedIn = false;
        showAuth().catch(() => {});
      }
      const error = new Error(typeof data.detail === "string" ? data.detail :
        `Request rejected (${response.status}). Check your input and connection.`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function view(name) {
    if (name === "account" && signedIn) setTimeout(() => execute(refreshAccount, {refresh: false}));
    document.querySelectorAll(".view").forEach((panel) => { panel.hidden = panel.id !== `view-${name}`; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      if (button.dataset.view === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  const SERVICES = {
    gmail: {icon: "✉", blurb: "Search, read, draft and send email.", actions: "sending and drafts"},
    google_calendar: {icon: "▦", blurb: "See your schedule, find free time, add and remove events.", actions: "creating and deleting events"},
    slack: {icon: "#", blurb: "Read channels, search messages and send to #channels or @people.", actions: "sending messages"},
    spotify: {icon: "♫", blurb: "Play any song, album, artist or playlist by name.", actions: "playback control"},
  };
  const SIGN_IN_HOSTS = ["accounts.google.com", "accounts.spotify.com", "slack.com"];
  let connectionData = {enabled: false, providers: [], accounts: [], activity: []};
  const signIns = {};  // provider → {url, started, allowActions}
  let signInPoll = null;

  function ago(seconds) {
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 60) return "just now";
    if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
    return new Date(seconds * 1000).toLocaleDateString([], {day: "numeric", month: "short"});
  }

  function shortIdentity(account) {
    // Slack identities are TEAM/USER IDs; show them compactly.
    return account.provider === "slack" ? `Workspace ${account.identity.split("/")[0]}` : account.identity;
  }

  function serviceState(provider, accounts) {
    if (!connectionData.enabled) return ["off", "Turned off"];
    if (!provider.configured) return ["setup", "Setup needed"];
    if (signIns[provider.provider]) return ["pending", "Waiting for sign-in"];
    const failed = connectionData.activity.find((item) => item.provider === provider.provider);
    if (accounts.length) return failed && !failed.ok ? ["warn", "Needs attention"] : ["on", "Connected"];
    return ["idle", "Not connected"];
  }

  function serviceCard(provider) {
    const info = SERVICES[provider.provider] || {icon: "•", blurb: "", actions: "actions"};
    const accounts = connectionData.accounts.filter((item) => item.provider === provider.provider);
    const [state, label] = serviceState(provider, accounts);
    const card = node("article", undefined, `service-card state-${state}`);
    const head = node("header");
    const title = node("div");
    title.append(node("h3", provider.name), node("span", label, `pill ${state}`));
    head.append(node("span", info.icon, `service-icon ${provider.provider}`), title);
    card.append(head, node("p", info.blurb, "service-blurb"));

    if (!connectionData.enabled) {
      card.append(node("p", "Set INTEGRATIONS_ENABLED=true in .env, then restart Bridge.", "hint"));
      return card;
    }
    if (!provider.configured) {
      const key = provider.provider === "slack" ? "SLACK_CLIENT_ID" : provider.provider === "spotify" ? "SPOTIFY_CLIENT_ID" : "GOOGLE_CLIENT_ID";
      card.append(node("p", `Add ${key} to .env and restart Bridge. Steps are in docs/SERVICES.md.`, "hint"));
      return card;
    }

    const writeScopes = new Set(provider.write_scopes);
    for (const account of accounts) {
      const row = node("div", undefined, "account-row");
      const who = node("div");
      const canAct = account.scopes.some((scope) => writeScopes.has(scope));
      const last = connectionData.activity.find((item) => item.provider === account.provider && item.account === account.identity && item.event !== "Connected");
      who.append(node("strong", shortIdentity(account)));
      const facts = [canAct ? "Read & actions" : "Read only"];
      if (account.connected_at) facts.push(`connected ${ago(account.connected_at)}`);
      if (last) facts.push(`last used ${ago(last.at)}`);
      who.append(node("span", facts.join(" · "), "account-facts"));
      if (last && !last.ok) who.append(node("span", `⚠ ${last.detail}`, "account-error"));
      if (!canAct) who.append(node("span", `Reconnect with “Allow ${info.actions}” to let Bridge act.`, "account-error"));
      row.append(who, action("Disconnect", () => {
        if (!window.confirm(`Disconnect ${shortIdentity(account)} from Bridge?`)) return;
        execute(async () => {
          const result = await request("/api/v1/connections/disconnect", "POST", {account_id: account.account_id});
          await refreshConnections();
          notify(result.message);
        }, {refresh: false});
      }));
      card.append(row);
    }

    if (!accounts.length && !signIns[provider.provider]) {
      const failure = connectionData.activity.find((item) => item.provider === provider.provider);
      if (failure && !failure.ok) card.append(node("p", `Last sign-in failed ${ago(failure.at)}: ${failure.detail}`, "account-error"));
    }
    const pending = signIns[provider.provider];
    const controls = node("div", undefined, "service-controls");
    if (pending) {
      const link = node("a", `Continue to ${provider.name} sign-in ↗`, "primary signin-link");
      link.href = pending.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const cancel = action("Cancel", () => { delete signIns[provider.provider]; renderConnections(); });
      controls.append(link, cancel);
      card.append(controls, node("p", "Sign in in the new tab. This card updates by itself when you're done.", "hint"));
      return card;
    }
    const toggle = node("label", undefined, "toggle");
    const box = node("input");
    box.type = "checkbox";
    box.checked = true;
    toggle.append(box, node("span", `Allow ${info.actions}`));
    const connect = action(accounts.length ? "Reconnect" : "Connect", () => startSignIn(provider, box.checked), accounts.length ? "secondary" : "primary");
    controls.append(toggle, connect);
    card.append(controls);
    const details = node("details");
    details.append(node("summary", "Permissions and callback address"),
      node("pre", `Callback: ${provider.redirect_uri}\n\nRead:\n${provider.read_scopes.join("\n")}\n\nActions:\n${provider.write_scopes.join("\n")}`));
    card.append(details);
    return card;
  }

  function renderActivity() {
    const filter = $("activity-filter").value;
    const names = Object.fromEntries(connectionData.providers.map((item) => [item.provider, item.name]));
    const items = connectionData.activity.filter((item) =>
      !filter || (filter === "failed" ? !item.ok : item.provider === filter));
    const list = $("connection-activity");
    list.replaceChildren();
    if (!items.length) list.append(node("li", filter ? "Nothing here yet." : "No activity yet. Connect a service and ask Bridge something.", "empty"));
    for (const item of items.slice(0, 60)) {
      const row = node("li", undefined, item.ok ? "ok" : "failed");
      row.append(node("span", (SERVICES[item.provider] || {}).icon || "•", `service-icon small ${item.provider}`));
      const text = node("div");
      text.append(node("strong", item.event), node("span", ` · ${names[item.provider] || item.provider}${item.account ? " · " + item.account : ""}`, "muted"));
      if (item.detail) text.append(node("p", item.detail, item.ok ? "muted" : "account-error"));
      row.append(text, node("time", ago(item.at)));
      list.append(row);
    }
  }

  function renderConnections() {
    const providers = connectionData.providers;
    const connected = providers.filter((p) => connectionData.accounts.some((a) => a.provider === p.provider)).length;
    $("connections-count").textContent = connectionData.enabled ? `${connected} of ${providers.length} services connected` : "Connected services are turned off";
    $("connections-meter").style.width = providers.length ? `${(connected / providers.length) * 100}%` : "0";
    $("connections-status").textContent = connectionData.enabled
      ? "Access tokens stay in macOS Keychain on this Mac."
      : connectionData.message || "";
    $("service-grid").replaceChildren(...providers.map(serviceCard));
    renderActivity();
  }

  async function refreshConnections() {
    connectionData = await request("/api/v1/connections");
    connectionData.activity = connectionData.activity || [];
    for (const provider of Object.keys(signIns)) {
      const started = signIns[provider].started;
      const done = connectionData.accounts.some((a) => a.provider === provider && a.connected_at >= started - 5);
      const failed = connectionData.activity.find((a) => a.provider === provider && a.at >= started && a.event === "Sign-in failed");
      if (done) { delete signIns[provider]; notify(`${provider.replace("_", " ")} connected.`); }
      else if (failed) { delete signIns[provider]; notify(failed.detail, true); }
      else if (Date.now() / 1000 - started > 300) delete signIns[provider];
    }
    if (!Object.keys(signIns).length && signInPoll) { clearInterval(signInPoll); signInPoll = null; }
    renderConnections();
  }

  function startSignIn(provider, allowActions) {
    execute(async () => {
      const result = await request("/api/v1/connections/start", "POST", {provider: provider.provider, allow_actions: allowActions});
      const url = new URL(result.authorization_url);
      if (url.protocol !== "https:" || !SIGN_IN_HOSTS.includes(url.hostname)) {
        throw new Error("Provider returned an unexpected sign-in address.");
      }
      signIns[provider.provider] = {url: url.href, started: Date.now() / 1000};
      renderConnections();
      // Watch for the finished sign-in without making the user refresh.
      if (!signInPoll) signInPoll = setInterval(() => { if (signedIn && !busy) refreshConnections().catch(() => {}); }, 3000);
    }, {refresh: false});
  }

  $("refresh-connections").addEventListener("click", () => execute(refreshConnections, {refresh: false}));
  $("activity-filter").addEventListener("change", renderActivity);

  const emptyChat = $("empty-chat").cloneNode(true);
  const loggedRequests = new Set();
  const toolLabels = {
    email_search: "Searched email", email_read_thread: "Read email thread", email_send: "Sent email",
    email_create_draft: "Saved draft", calendar_events: "Checked Google Calendar",
    mac_calendar_events: "Checked calendar", mac_calendar_free_time: "Found free time",
    mac_calendar_create_event: "Added event", daily_briefing: "Prepared briefing",
    reminders_add: "Added reminder", reminders_list: "Checked reminders",
    spotify_play_search: "Played on Spotify", spotify_control: "Controlled Spotify",
    slack_send_message: "Sent Slack message", slack_read_channel: "Read Slack",
  };

  function toolLabel(name) {
    if (toolLabels[name]) return toolLabels[name];
    const words = String(name || "step").replaceAll("_", " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function stepChips(steps) {
    const list = node("ul", undefined, "step-chips");
    for (const step of steps) {
      const state = step.success ? "done" : step.status === "confirmation_required" ? "waiting" : "failed";
      const icon = state === "done" ? "✓" : state === "waiting" ? "◷" : "✕";
      list.append(node("li", `${icon} ${toolLabel(step.tool)}`, `chip ${state}`));
    }
    return list;
  }

  function chatMessage(author, text, steps, meta = {}) {
    const empty = $("empty-chat");
    if (empty) empty.remove();
    const card = node("article", undefined, `message ${author === "You" ? "user" : "assistant"}`);
    const header = node("div", undefined, "author");
    header.append(node("span", author.toUpperCase()));
    if (meta.at) {
      const time = new Date(meta.at);
      header.append(node("time", time.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})));
    }
    card.append(header, node("p", text));
    if (steps && steps.length) {
      card.append(stepChips(steps));
      if (steps.some((step) => "result" in step || "error" in step)) {
        const details = node("details");
        details.append(node("summary", "Technical details"), node("pre", JSON.stringify(steps, null, 2)));
        card.append(details);
      }
    }
    $("conversation").append(card);
    $("conversation").scrollTop = $("conversation").scrollHeight;
  }

  function renderConversation(messages) {
    $("conversation").replaceChildren();
    loggedRequests.clear();
    if (!messages.length) $("conversation").append(emptyChat.cloneNode(true));
    for (const entry of messages) {
      if (entry.request_id) loggedRequests.add(entry.request_id);
      chatMessage(entry.role === "user" ? "You" : "Bridge", entry.text, entry.steps, entry);
    }
    wireExamples();
  }

  async function loadConversation() {
    renderConversation((await request("/api/v1/conversation")).messages);
  }

  function showResult(result) {
    // Chat requests are already rendered from the server's conversation log.
    const logged = loggedRequests.has(result.request_id);
    if (!logged) chatMessage("Bridge", result.message, result.steps);
    // Chat replies are already on screen; only repeat them in the banner when something failed.
    if (!logged || result.status === "failed") notify(result.message, result.status === "failed");
    if (result.status === "confirmation_required") {
      pending = result.confirmation;
      $("confirmation-title").textContent = pending.action.replaceAll("_", " ");
      $("confirmation-message").textContent = result.message;
      $("confirmation-arguments").textContent = JSON.stringify(pending.arguments, null, 2);
      $("confirmation-error").textContent = "";
      $("review-workflows").hidden = true;
      if (!dialog.open) dialog.showModal();
    }
  }

  async function execute(task, {refresh = true} = {}) {
    if (busy) return;
    busy = true;
    syncControls();
    notify("");
    try {
      const result = await task();
      if (result && result.request_id) showResult(result);
      if (refresh && signedIn) await refreshAll();
    } catch (error) {
      notify(error.message, true);
      if (pending) {
        $("confirmation-error").textContent = `${error.message} You can leave this review and inspect workflows.`;
        $("review-workflows").hidden = false;
      }
    } finally {
      busy = false;
      syncControls();
    }
  }

  function action(label, handler, className = "secondary") {
    const button = node("button", label, className);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  function renderProjects(projects) {
    $("project-list").replaceChildren();
    if (!projects.length) $("project-list").append(node("p", "No projects saved yet. Add your first workspace."));
    for (const project of projects) {
      const card = node("article", undefined, "list-card");
      card.append(node("h3", project.name), node("p", project.path));
      const actions = node("div", undefined, "actions");
      actions.append(action("Edit", () => {
        $("project-name").value = project.name;
        $("project-path").value = project.path;
        $("project-path").focus();
      }), action("Forget", () => execute(() => request("/api/v1/projects/forget", "POST", {name: project.name}))));
      card.append(actions);
      $("project-list").append(card);
    }
  }

  function statusBadge(status) {
    const badge = node("span", status.replaceAll("_", " "), "status");
    // Only known status names are used as CSS classes.
    if (["failed", "interrupted", "paused", "confirmation_required", "warning"].includes(status)) badge.classList.add(status);
    return badge;
  }

  function renderWorkflows(workflows) {
    $("workflow-list").replaceChildren();
    if (!workflows.length) $("workflow-list").append(node("p", "No workflows yet. Send a request to get started."));
    for (const workflow of workflows) {
      const card = node("article", undefined, "list-card");
      card.append(statusBadge(workflow.status), node("p", workflow.request_id, "workflow-id"),
        node("p", `${workflow.updated_at} UTC`), action("Review steps", () => execute(async () => {
          selectedWorkflow = workflow.request_id;
          renderWorkflow(await request(`/api/v1/workflows/${encodeURIComponent(selectedWorkflow)}`));
        }, {refresh: false})));
      $("workflow-list").append(card);
    }
  }

  async function refreshWorkflows() {
    const query = new URLSearchParams({offset: workflowOffset, limit: workflowLimit});
    if ($("workflow-filter").value) query.set("status", $("workflow-filter").value);
    let data = await request(`/api/v1/workflows?${query}`);
    // Cleanup or another tab may remove the last page while it is selected.
    if (workflowOffset > 0 && !data.workflows.length) {
      workflowOffset = Math.max(0, Math.ceil(data.total / workflowLimit) - 1) * workflowLimit;
      query.set("offset", workflowOffset);
      data = await request(`/api/v1/workflows?${query}`);
    }
    workflowTotal = data.total;
    renderWorkflows(data.workflows);
    $("workflow-page").textContent = data.total ?
      `${data.offset + 1}–${data.offset + data.workflows.length} of ${data.total}` : "No records";
    $("workflow-prev").disabled = workflowOffset === 0;
    $("workflow-next").disabled = !data.has_more;
    if (selectedWorkflow) {
      try {
        renderWorkflow(await request(`/api/v1/workflows/${encodeURIComponent(selectedWorkflow)}`));
      } catch (error) {
        if (error.status !== 404) throw error;
        selectedWorkflow = null;
        $("workflow-detail").replaceChildren(node("h2", "Record no longer available"),
          node("p", "This record was removed. Choose another workflow to review."));
      }
    }
  }

  function renderWorkflow(workflow) {
    const panel = $("workflow-detail");
    panel.replaceChildren(node("h2", "Workflow review"), statusBadge(workflow.status),
      node("p", workflow.request_id, "workflow-id"));
    panel.append(node("h3", "Recorded steps"));
    const steps = node("ol", undefined, "step-list");
    for (const step of workflow.steps) steps.append(node("li", `${step.tool}: ${step.success ? "succeeded" : "failed"}`));
    panel.append(steps);
    if (!workflow.steps.length) panel.append(node("p", "No completed steps recorded."));
    panel.append(node("h3", "Remaining plan"), node("pre", JSON.stringify(workflow.queue, null, 2)));
    const resumable = ["paused", "confirmation_required"].includes(workflow.status) && workflow.recoverable && !workflow.in_flight;
    if (resumable) {
      panel.append(node("p", "Resuming runs the remaining saved steps. Actions requiring approval will pause again."),
        action("Resume saved steps", () => execute(() => request(`/api/v1/workflows/${encodeURIComponent(workflow.request_id)}/resume`, "POST")), "primary"));
    } else if (workflow.status === "interrupted" || workflow.recoverable === false) {
      panel.append(node("p", "This workflow cannot be replayed. An outcome may be uncertain or arguments were omitted. Inspect your Mac before making a new request."));
    }
    if (["paused", "confirmation_required", "interrupted"].includes(workflow.status)) {
      panel.append(action("Cancel remaining work", () => execute(() => request(`/api/v1/workflows/${encodeURIComponent(workflow.request_id)}/cancel`, "POST"))));
    }
  }

  async function refreshAll() {
    const [projects, preferences] = await Promise.all([
      request("/api/v1/projects"), request("/api/v1/preferences"), refreshWorkflows(), refreshTasks(),
      refreshCapabilities(), refreshDiagnostics(), loadConversation(), refreshMemories(), refreshProactive(),
      refreshConnections().catch(() => {})
    ]);
    renderProjects(projects.projects);
    $("default-browser").value = preferences.default_browser;
    $("default-editor").value = preferences.default_editor;
  }

  async function refreshProactive() {
    const data = await request("/api/v1/proactive");
    const settings = data.settings;
    $("pro-meetings").checked = settings.meeting_prep;
    $("pro-lead").value = String(settings.lead_minutes);
    $("pro-evening").checked = settings.evening_summary;
    $("pro-evening-time").value = settings.evening_time;
    const list = $("watch-list");
    list.replaceChildren();
    if (!data.watches.length) list.append(node("p", "Nothing yet. Add a Gmail or Slack search below.", "hint"));
    for (const watch of data.watches) {
      const row = node("div", undefined, "method-row");
      const text = node("div");
      const checked = watch.last_checked ? `checked ${ago(watch.last_checked)}` : "not checked yet";
      text.append(node("strong", watch.label), node("span", `${watch.kind === "email" ? "Gmail" : "Slack"}: ${watch.query} · ${checked}`, "account-facts"));
      if (watch.last_error) text.append(node("span", `⚠ ${watch.last_error}`, "account-error"));
      row.append(node("span", watch.kind === "email" ? "✉" : "#", "method-icon"), text, action("Stop", () => execute(async () => {
        await request("/api/v1/watches/remove", "POST", {id: watch.id});
        await refreshProactive();
      }, {refresh: false})));
      list.append(row);
    }
  }

  function saveProactive() {
    execute(async () => {
      await request("/api/v1/proactive/settings", "POST", {
        meeting_prep: $("pro-meetings").checked, lead_minutes: Number($("pro-lead").value),
        evening_summary: $("pro-evening").checked, evening_time: $("pro-evening-time").value || "18:00",
      });
      await refreshProactive();
      notify("Proactive settings saved.");
    }, {refresh: false});
  }
  for (const id of ["pro-meetings", "pro-lead", "pro-evening", "pro-evening-time"]) $(id).addEventListener("change", saveProactive);

  $("watch-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const kind = $("watch-kind").value;
    const query = $("watch-query").value.trim();
    const label = kind === "slack" && /^(mentions|@me|me)$/i.test(query) ? "Slack mentions of me" : `${kind === "email" ? "Emails" : "Slack messages"}: ${query}`;
    execute(async () => {
      await request("/api/v1/watches", "POST", {kind, query, label});
      $("watch-query").value = "";
      await refreshProactive();
    }, {refresh: false});
  });

  async function refreshMemories() {
    const data = await request("/api/v1/memories");
    const list = $("memory-list");
    list.replaceChildren();
    if (!data.memories.length) list.append(node("p", "Nothing yet. Add a fact above or say “remember that …”.", "hint"));
    for (const fact of [...data.memories].reverse()) {
      const row = node("div", undefined, "method-row");
      const text = node("div");
      text.append(node("strong", fact.text), node("span", `Remembered ${ago(fact.created_at)}`, "account-facts"));
      row.append(node("span", "✦", "method-icon"), text, action("Forget", () => execute(async () => {
        await request("/api/v1/memories/forget", "POST", {id: fact.id});
        await refreshMemories();
      }, {refresh: false})));
      list.append(row);
    }
    const remote = authStatus && document.getElementById("provider-summary").textContent.includes("OpenAI");
    $("memory-privacy").textContent = "Bridge won't store passwords, codes or card numbers. You can also say “remember that …” or “forget …” in the conversation." +
      (remote ? " With OpenAI selected, memories are sent along with each request." : "");
  }

  $("memory-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(async () => {
      await request("/api/v1/memories", "POST", {text: $("memory-text").value});
      $("memory-text").value = "";
      await refreshMemories();
    }, {refresh: false});
  });

  function renderCapabilities() {
    const query = $("capability-search").value.trim().toLowerCase();
    const matches = capabilities.filter((tool) =>
      `${tool.name} ${tool.description}`.toLowerCase().includes(query));
    $("capability-count").textContent = `${matches.length} of ${capabilities.length} capabilities`;
    $("capability-list").replaceChildren();
    if (!matches.length) $("capability-list").append(node("p", "No capabilities match your search."));
    for (const tool of matches) {
      const card = node("article", undefined, "list-card");
      card.append(node("h3", tool.name), node("p", tool.description),
        node("span", `Base risk: ${tool.risk}`, "status"));
      if (tool.dynamic_policy) card.append(node("p", "Dynamic policy: arguments determine additional security checks."));
      if (tool.confirmation_message) card.append(node("p", `Approval: ${tool.confirmation_message}`));
      card.append(node("p", tool.arguments_persisted ?
        "Arguments are included in the local workflow journal." :
        "Arguments are omitted from the local workflow journal.", "hint"));
      const schema = node("details");
      schema.append(node("summary", "Input schema"), node("pre", JSON.stringify(tool.input_schema, null, 2)));
      card.append(schema);
      $("capability-list").append(card);
    }
  }

  async function refreshCapabilities() {
    const data = await request("/api/v1/capabilities");
    capabilities = data.tools;
    renderCapabilities();
  }

  async function refreshDiagnostics() {
    const data = await request("/api/v1/diagnostics");
    renderProvider(data.provider);
    $("diagnostics-summary").textContent = data.ready ?
      "Local readiness checks passed." : "Some local checks need attention.";
    $("diagnostics-list").replaceChildren();
    for (const check of data.checks) {
      const card = node("article", undefined, "list-card");
      card.append(node("h3", check.name), statusBadge(check.status), node("p", check.message));
      $("diagnostics-list").append(card);
    }
  }

  function renderProvider(provider) {
    const name = "OpenAI";
    $("provider-summary").textContent = `${name} (${provider.model}) handles language requests.`;
    let policy;
    if (provider.remote_tool_results === "all") {
      policy = "Messages and all tool results are sent to OpenAI.";
    } else if (provider.remote_tool_results === "allowlist") {
      policy = "Messages go to OpenAI. OpenAI can read results from the tools listed below; other results share only whether they succeeded.";
    } else {
      policy = "Messages go to OpenAI. Tool results share only whether they succeeded; their contents stay on this Mac.";
    }
    $("provider-privacy-hint").textContent = `${policy} Screenshot images are not uploaded.`;
    const panel = $("provider-details");
    panel.replaceChildren(node("h3", name), node("p", `Model: ${provider.model}`), node("p", policy));
    panel.append(node("p", `Tool-result sharing: ${provider.remote_tool_results}`));
    if (provider.remote_tool_results === "allowlist") {
      panel.append(node("p", `OpenAI can read: ${provider.remote_tool_result_allowlist.join(", ") || "nothing"}`));
    }
    panel.append(node("p", "Avoid including secrets in requests.", "hint"));
  }


  async function refreshTasks() {
    const data = await request("/api/v1/tasks");
    $("task-list").replaceChildren();
    if (!data.tasks.length) $("task-list").append(node("p", "No live tasks in this session."));
    for (const task of data.tasks) {
      const card = node("article", undefined, "list-card");
      card.append(statusBadge(task.status), node("p", task.request_id, "workflow-id"),
        action("Follow task", () => execute(async () => watchTask(
          await request(`/api/v1/tasks/${encodeURIComponent(task.request_id)}`)
        ))));
      $("task-list").append(card);
    }
  }

  // ---- Sign-in ------------------------------------------------------------------------

  let authStatus = null;
  const PROVIDER_NAMES = {google: "Google", github: "GitHub"};

  function authError(message) {
    $("auth-error").textContent = message || "";
  }

  function socialButtons(container, intent) {
    container.replaceChildren();
    for (const provider of ["google", "github"]) {
      const method = authStatus.methods[provider];
      if (!method.configured || (intent === "login" && !method.linked)) continue;
      const button = node("button", `Continue with ${PROVIDER_NAMES[provider]}`, `social ${provider}`);
      button.type = "button";
      button.addEventListener("click", () => startSocial(provider, intent));
      container.append(button);
    }
    return container.children.length;
  }

  async function startSocial(provider, intent) {
    authError("");
    try {
      const {url} = await request(`/api/v1/auth/oauth/${provider}/start`, "POST",
        {intent, remember: intent === "link" ? false : $("login-remember").checked});
      const target = new URL(url);
      if (target.protocol !== "https:" || !["accounts.google.com", "github.com"].includes(target.hostname)) {
        throw new Error("Unexpected sign-in address.");
      }
      window.location.assign(target.href);
    } catch (error) {
      if (intent === "link") notify(error.message, true); else authError(error.message);
    }
  }

  async function showAuth() {
    authStatus = await request("/api/v1/auth/status");
    if (authStatus.signed_in) return enterWorkspace();
    signedIn = false;
    document.body.classList.add("signed-out");
    $("workspace").hidden = true;
    $("auth-panel").hidden = false;
    $("auth-loading").hidden = true;
    $("account-chip").hidden = true;
    $("disconnect").hidden = true;
    $("connection-status").textContent = "Signed out";
    $("connection-status").classList.remove("online");
    const needsSetup = !authStatus.has_owner;
    const canSetup = needsSetup && (authStatus.setup || setupToken);
    $("signup-form").hidden = !canSetup;
    $("auth-need-menu").hidden = !(needsSetup && !canSetup);
    $("login-form").hidden = needsSetup;
    if (canSetup) {
      const social = socialButtons($("signup-social"), "signup");
      $("signup-form").querySelector(".divider").hidden = !social;
      $("signup-name").focus();
    } else if (!needsSetup) {
      const social = socialButtons($("login-social"), "login");
      const passkey = authStatus.methods.passkey && authStatus.passkeys_supported && window.PublicKeyCredential;
      $("passkey-login").hidden = !passkey;
      $("login-password").hidden = !authStatus.methods.password;
      $("login-form").querySelector("button[type=submit]").className = passkey ? "secondary wide" : "primary wide";
      $("login-divider").hidden = !(authStatus.methods.password && (social || passkey));
      if (authStatus.methods.password) $("login-email").focus();
    }
  }

  async function enterWorkspace() {
    signedIn = true;
    setupToken = "";
    const status = authStatus && authStatus.signed_in ? authStatus : await request("/api/v1/auth/status");
    authStatus = status;
    document.body.classList.remove("signed-out");
    $("auth-panel").hidden = true;
    $("workspace").hidden = false;
    $("disconnect").hidden = false;
    $("account-chip").hidden = false;
    const owner = status.owner || {name: "", email: ""};
    $("account-name").textContent = owner.name || owner.email;
    $("account-initials").textContent = (owner.name || owner.email || "?").trim().split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
    $("connection-status").textContent = "Signed in";
    $("connection-status").classList.add("online");
    syncControls();
    await refreshAll();
    $("message").focus();
  }

  $("signup-form").addEventListener("submit", (event) => {
    event.preventDefault();
    authError("");
    execute(async () => {
      await request("/api/v1/auth/signup", "POST", {
        name: $("signup-name").value, email: $("signup-email").value, password: $("signup-password").value,
      });
      $("signup-password").value = "";
      authStatus = null;
      await enterWorkspace();
      notify("Welcome to Bridge! Add Touch ID or link Google/GitHub on the Account page.");
    }, {refresh: false}).then(() => authError($("notice").classList.contains("error") ? $("notice").textContent : ""));
  });

  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    authError("");
    execute(async () => {
      await request("/api/v1/auth/login", "POST", {
        email: $("login-email").value, password: $("login-secret").value, remember: $("login-remember").checked,
      });
      $("login-secret").value = "";
      authStatus = null;
      await enterWorkspace();
    }, {refresh: false}).then(() => authError($("notice").classList.contains("error") ? $("notice").textContent : ""));
  });

  $("token-form").addEventListener("submit", (event) => {
    event.preventDefault();
    setupToken = $("api-token").value.trim();
    $("api-token").value = "";
    showAuth().catch((error) => authError(error.message));
  });

  // WebAuthn wants ArrayBuffers; the server speaks base64url JSON.
  const fromB64 = (value) => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=")), (c) => c.charCodeAt(0)).buffer;
  const toB64 = (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  function credentialJSON(credential) {
    const response = {clientDataJSON: toB64(credential.response.clientDataJSON)};
    if (credential.response.attestationObject) {
      response.attestationObject = toB64(credential.response.attestationObject);
      if (credential.response.getTransports) response.transports = credential.response.getTransports();
    } else {
      response.authenticatorData = toB64(credential.response.authenticatorData);
      response.signature = toB64(credential.response.signature);
      if (credential.response.userHandle) response.userHandle = toB64(credential.response.userHandle);
    }
    return {id: credential.id, rawId: toB64(credential.rawId), type: credential.type, response,
      authenticatorAttachment: credential.authenticatorAttachment || undefined,
      clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {}};
  }

  $("passkey-login").addEventListener("click", () => {
    authError("");
    execute(async () => {
      const options = await request("/api/v1/auth/passkey-login/options", "POST");
      options.challenge = fromB64(options.challenge);
      options.allowCredentials = (options.allowCredentials || []).map((item) => ({...item, id: fromB64(item.id)}));
      const credential = await navigator.credentials.get({publicKey: options});
      await request("/api/v1/auth/passkey-login", "POST", {credential: credentialJSON(credential), remember: $("login-remember").checked});
      authStatus = null;
      await enterWorkspace();
    }, {refresh: false}).then(() => authError($("notice").classList.contains("error") ? $("notice").textContent : ""));
  });

  async function addPasskey() {
    const options = await request("/api/v1/auth/passkeys/options", "POST");
    options.challenge = fromB64(options.challenge);
    options.user.id = fromB64(options.user.id);
    options.excludeCredentials = (options.excludeCredentials || []).map((item) => ({...item, id: fromB64(item.id)}));
    let credential;
    try {
      credential = await navigator.credentials.create({publicKey: options});
    } catch (error) {
      throw new Error(error.name === "InvalidStateError" ? "This Mac already has a Bridge passkey." : "Passkey setup was cancelled.");
    }
    await request("/api/v1/auth/passkeys", "POST", {credential: credentialJSON(credential), name: "Touch ID on this Mac"});
    notify("Passkey added. Next time, sign in with Touch ID.");
  }

  // ---- Account page -------------------------------------------------------------------

  function browserName(agent) {
    if (/Edg\//.test(agent)) return "Edge";
    if (/Chrome\//.test(agent)) return "Chrome";
    if (/Firefox\//.test(agent)) return "Firefox";
    if (/Safari\//.test(agent)) return "Safari";
    return agent ? "Browser" : "Bridge";
  }

  async function refreshAccount() {
    const data = await request("/api/v1/auth/account");
    $("profile-name").value = data.owner.name;
    $("profile-email").value = data.owner.email;
    $("password-title").textContent = data.owner.has_password ? "Password" : "Add a password";
    $("password-current-row").hidden = !data.owner.has_password;
    const methods = $("sign-in-methods");
    methods.replaceChildren();
    const row = (icon, title, detail, button) => {
      const item = node("div", undefined, "method-row");
      const text = node("div");
      text.append(node("strong", title), node("span", detail, "account-facts"));
      item.append(node("span", icon, "method-icon"), text);
      if (button) item.append(button);
      methods.append(item);
    };
    row("✱", "Password", data.owner.has_password ? "Set" : "Not set", null);
    for (const provider of ["google", "github"]) {
      const identity = data.identities.find((item) => item.provider === provider);
      if (identity) {
        row(provider === "google" ? "G" : "GH", PROVIDER_NAMES[provider], `Linked · ${identity.login || identity.email}`,
          action("Unlink", () => execute(async () => {
            await request("/api/v1/auth/unlink", "POST", {value: provider});
            await refreshAccount();
          }, {refresh: false})));
      } else if (data.providers[provider]) {
        row(provider === "google" ? "G" : "GH", PROVIDER_NAMES[provider], "Not linked", action(`Link ${PROVIDER_NAMES[provider]}`, () => startSocial(provider, "link")));
      } else {
        row(provider === "google" ? "G" : "GH", PROVIDER_NAMES[provider], `Add ${provider.toUpperCase()}_CLIENT_ID to .env to enable`, null);
      }
    }
    for (const key of data.passkeys) {
      row("⌘", key.name, `Passkey · added ${ago(key.created_at)}${key.last_used ? " · used " + ago(key.last_used) : ""}`,
        action("Remove", () => execute(async () => {
          await request("/api/v1/auth/passkeys/remove", "POST", {value: key.credential_id});
          await refreshAccount();
        }, {refresh: false})));
    }
    const supported = window.PublicKeyCredential && window.location.hostname === "localhost";
    const add = action("Add Touch ID passkey", () => execute(async () => { await addPasskey(); await refreshAccount(); }, {refresh: false}), "primary");
    add.disabled = !supported;
    row("⌘", "Passkey", supported ? "Sign in with Touch ID or your iCloud Keychain." : "Open the dashboard at http://localhost:8000 to use passkeys.", add);

    const sessions = $("session-list");
    sessions.replaceChildren();
    for (const item of data.sessions) {
      const entry = node("div", undefined, "method-row");
      const text = node("div");
      text.append(node("strong", `${browserName(item.user_agent)}${item.current ? " · this window" : ""}`),
        node("span", `Signed in ${ago(item.created_at)} · active ${ago(item.last_seen)}${item.remember ? " · remembered" : ""}`, "account-facts"));
      entry.append(node("span", "◉", "method-icon"), text);
      if (!item.current) entry.append(action("Sign out", () => execute(async () => {
        await request("/api/v1/auth/sessions/end", "POST", {value: item.id});
        await refreshAccount();
      }, {refresh: false})));
      sessions.append(entry);
    }
    $("end-other-sessions").disabled = data.sessions.length < 2;
  }

  $("profile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(async () => {
      await request("/api/v1/auth/profile", "POST", {name: $("profile-name").value, email: $("profile-email").value});
      authStatus = await request("/api/v1/auth/status");
      $("account-name").textContent = authStatus.owner.name;
      notify("Profile saved.");
    }, {refresh: false});
  });
  $("password-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(async () => {
      await request("/api/v1/auth/password", "POST", {current: $("password-current").value, new: $("password-new").value});
      $("password-current").value = "";
      $("password-new").value = "";
      await refreshAccount();
      notify("Password saved.");
    }, {refresh: false});
  });
  $("end-other-sessions").addEventListener("click", () => execute(async () => {
    const result = await request("/api/v1/auth/sessions/end-others", "POST");
    await refreshAccount();
    notify(`Signed out ${result.ended} other session${result.ended === 1 ? "" : "s"}.`);
  }, {refresh: false}));
  $("account-chip").addEventListener("click", () => { view("account"); execute(refreshAccount, {refresh: false}); });

  // ---- Start-up: menu-bar link, provider return, or existing session ----------------

  async function handleHash() {
    const hash = new URLSearchParams(window.location.hash.slice(1));
    if ([...hash.keys()].length) history.replaceState(null, "", window.location.pathname);
    if (hash.get("auth-error")) authError(hash.get("auth-error"));
    const launch = hash.get("launch");
    if (launch) {
      const response = await fetch("/api/v1/session/launch", {method: "POST", credentials: "same-origin",
        cache: "no-store", redirect: "error", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ticket: launch})}).catch(() => null);
      if (!response || !response.ok) {
        const data = response ? await response.json().catch(() => ({})) : {};
        authError(typeof data.detail === "string" ? data.detail : "That link expired. Open the dashboard again from the Bridge menu.");
      }
    }
    await showAuth();
    if (hash.get("account-linked") && signedIn) {
      view("account");
      await refreshAccount();
      notify(`${PROVIDER_NAMES[hash.get("account-linked")] || "Account"} linked. You can now sign in with it.`);
    }
  }
  window.addEventListener("hashchange", () => handleHash().catch((error) => authError(error.message)));
  handleHash().catch((error) => { $("auth-loading").hidden = true; authError(error.message); });

  $("disconnect").addEventListener("click", () => {
    if (busy || pending) return;
    fetch("/api/v1/auth/logout", {method: "POST", credentials: "same-origin"}).finally(() => window.location.reload());
  });

  $("message-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const message = $("message").value.trim();
    if (!message || busy || pending) return;
    $("message").value = "";
    chatMessage("You", message);
    execute(async () => {
      const result = await watchTask(await request("/api/v1/tasks", "POST", {message}));
      await loadConversation();
      return result;
    });
  });

  function renderProgress(progress) {
    const thinking = !progress.current_tool && !progress.remaining_tools.length;
    $("task-progress").textContent = progress.cancel_requested ? "Stopping after the current operation." :
      thinking ? (progress.steps.length ? "Deciding what to do next…" : "Understanding your request…") :
      "Working on it…";
    const list = $("task-steps");
    list.replaceChildren();
    for (const step of progress.steps) {
      list.append(node("li", toolLabel(step.tool), step.success ? "done" : "failed"));
    }
    if (progress.current_tool) list.append(node("li", toolLabel(progress.current_tool), "running"));
    for (const tool of progress.remaining_tools) list.append(node("li", toolLabel(tool), "next"));
  }

  async function watchTask(progress) {
      liveTask = progress.request_id;
      $("live-task").hidden = false;
      $("stop-task").disabled = false;
      try {
        while (!progress.result) {
          renderProgress(progress);
          await new Promise((resolve) => setTimeout(resolve, 500));
          progress = await request(`/api/v1/tasks/${encodeURIComponent(liveTask)}`);
          if (progress.status === "interrupted") throw new Error("Task interrupted. Review saved workflows.");
        }
        return progress.result;
      } finally {
        liveTask = null;
        $("live-task").hidden = true;
      }
  }

  $("stop-task").addEventListener("click", async () => {
    if (!liveTask) return;
    $("stop-task").disabled = true;
    try {
      await request(`/api/v1/tasks/${encodeURIComponent(liveTask)}/cancel`, "POST");
    } catch (error) {
      notify(error.message, true);
      $("stop-task").disabled = false;
    }
  });

  $("project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(() => request("/api/v1/projects", "POST", {
      name: $("project-name").value, path: $("project-path").value
    }));
  });

  $("preferences-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(() => request("/api/v1/preferences", "POST", {
      default_browser: $("default-browser").value, default_editor: $("default-editor").value
    }));
  });

  function decide(approved) {
    if (!pending || busy) return;
    execute(async () => {
      const progress = await request("/api/v1/tasks/confirm", "POST", {token: pending.token, approved});
      pending = null;
      dialog.close();
      const result = await watchTask(progress);
      await loadConversation();
      return result;
    });
  }
  $("approve").addEventListener("click", () => decide(true));
  $("decline").addEventListener("click", () => decide(false));
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); decide(false); });
  $("review-workflows").addEventListener("click", () => {
    pending = null;
    dialog.close();
    view("workflows");
    execute(async () => {});
  });
  $("reset-chat").addEventListener("click", () => execute(async () => {
    await request("/api/v1/conversation/reset", "POST");
    renderConversation([]);
    notify("Conversation cleared. Saved projects and workflows are unchanged.");
  }));
  $("refresh-projects").addEventListener("click", () => execute(async () => {}));
  $("capability-search").addEventListener("input", renderCapabilities);
  $("refresh-diagnostics").addEventListener("click", () => execute(refreshDiagnostics, {refresh: false}));
  $("refresh-workflows").addEventListener("click", () => execute(async () => {}));
  $("workflow-filter").addEventListener("change", () => {
    workflowOffset = 0;
    execute(refreshWorkflows, {refresh: false});
  });
  $("workflow-prev").addEventListener("click", () => {
    if (busy || workflowOffset === 0) return;
    workflowOffset = Math.max(0, workflowOffset - workflowLimit);
    execute(refreshWorkflows, {refresh: false});
  });
  $("workflow-next").addEventListener("click", () => {
    if (busy || workflowOffset + workflowLimit >= workflowTotal) return;
    workflowOffset += workflowLimit;
    execute(refreshWorkflows, {refresh: false});
  });
  $("retention-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(() => request("/api/v1/workflows/cleanup/preview", "POST", {
      older_than_days: Number($("retention-days").value),
      keep_recent: Number($("retention-keep").value)
    }));
  });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => view(button.dataset.view)));
  function wireExamples() {
    document.querySelectorAll("[data-example]").forEach((button) => { button.onclick = () => {
      if (button.dataset.go) view(button.dataset.go);
      $("message").value = button.dataset.example;
      $("message").focus();
    }; });
  }
  wireExamples();
  // Requests from the panel, voice or schedules show up when you come back to this tab.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && signedIn && !busy && !pending) {
      loadConversation().catch(() => {});
    }
  });
})();
