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

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  // A floating toast: it never pushes the screen around. Good news fades; errors stay.
  let noticeTimer = null;
  function notify(message, error = false) {
    $("notice").textContent = message;
    $("notice").className = error ? "error" : "";
    $("notice").hidden = !message;
    clearTimeout(noticeTimer);
    if (message && !error) noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 6000);
  }
  $("notice").addEventListener("click", () => { $("notice").hidden = true; });

  function syncControls() {
    $("workspace").disabled = busy || !signedIn;
    $("disconnect").disabled = busy || Boolean(pending);
    // While an approval waits, the composer waits too: answer the card first.
    $("message").disabled = Boolean(pending);
    $("send-message").disabled = busy || Boolean(pending);
    $("message").placeholder = pending ? "Answer the card above first…"
      : phoneMode() ? "Ask Bridge…" : "Ask anything, or describe what you want done…";
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

  // On the phone: Today, Ask, Inbox and You. Everything else stays on the Mac.
  const PHONE_VIEWS = ["today", "chat", "inbox", "phone"];
  const phoneMode = () => Boolean(authStatus && authStatus.phone);

  function view(name, section) {
    if (phoneMode() && !PHONE_VIEWS.includes(name)) name = "today";
    if (name === "phone" && window.BridgePhone) window.BridgePhone.showYou();
    document.body.classList.toggle("onboarding-mode", name === "onboarding");
    if (name === "account") { name = "settings"; section = "account"; }
    if (name === "settings" && window.BridgeSettings) window.BridgeSettings.show(section || window.BridgeSettings.current());
    if (name === "memory" && signedIn) setTimeout(() => execute(refreshMemories, {refresh: false}));
    if (name === "today" && signedIn && window.BridgeToday) window.BridgeToday.refresh();
    if (name === "inbox" && signedIn && window.BridgeInbox) window.BridgeInbox.refresh();
    if (name === "automations" && signedIn && window.BridgeAutomations) window.BridgeAutomations.refresh();
    if (name === "chat" && signedIn) {
      setTimeout(() => {
        if (!phoneMode()) peekScreen();
        scrollThread();
        if (!pending && !phoneMode()) $("message").focus();  // No keyboard popping up on the phone.
      });
    }
    document.querySelectorAll(".view").forEach((panel) => { panel.hidden = panel.id !== `view-${name}`; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      if (button.dataset.view === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  // What each service lets Bridge do, in plain words. These follow the scopes Bridge asks for.
  const SERVICES = {
    gmail: {glyph: "M", tone: "warm", actions: "sending replies and drafts",
      reads: ["Your inbox, to sort it, find threads and see who replied"],
      does: ["Send an email or a reply", "Save a draft"],
      never: ["Delete email or change your labels and Gmail settings"]},
    google_calendar: {glyph: "C", tone: "cool", actions: "creating and deleting events",
      reads: ["Your Google calendars and free time"],
      does: ["Create events and send invitations", "Delete an event you name"],
      never: ["Accept or decline invitations for you"]},
    slack: {glyph: "#", tone: "violet", actions: "sending messages and setting your status",
      reads: ["Channels and messages you can see, to search and summarize", "Mentions of you"],
      does: ["Send a message", "Set your status and pause notifications while you focus"],
      never: ["Change workspace settings or anyone else's account"]},
    jira: {glyph: "J", tone: "cool", actions: "creating issues, commenting and moving status",
      reads: ["Issues assigned to you and issues you search for", "Confluence pages you can see"],
      does: ["Create an issue", "Comment on an issue", "Move an issue to another status"],
      never: ["Delete issues or pages, or change project settings"]},
    github: {glyph: "GH", tone: "", actions: "creating issues and commenting",
      reads: ["Pull requests waiting for your review", "Your pull requests and their checks", "Issues assigned to you"],
      does: ["Create an issue", "Comment on an issue or pull request"],
      never: ["Merge, approve, push code or change repository settings"]},
    spotify: {glyph: "♪", tone: "green", actions: "playing music",
      reads: ["Your playlists and what's playing"],
      does: ["Play, pause and skip music"],
      never: ["Change your playlists or follow anyone"]},
  };
  const SIGN_IN_HOSTS = ["accounts.google.com", "accounts.spotify.com", "slack.com"];
  let connectionData = {enabled: false, providers: [], accounts: [], activity: []};
  const signIns = {};  // provider → {url, started}
  let signInPoll = null;
  let chosenService = null;

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
    if (signIns[provider.provider]) return ["pending", "Signing in…"];
    const last = connectionData.activity.find((item) => item.provider === provider.provider);
    if (accounts.length) return last && !last.ok ? ["warn", "Needs attention"] : ["on", "Connected"];
    return ["idle", "Not connected"];
  }

  const accountsFor = (provider) => connectionData.accounts.filter((item) => item.provider === provider.provider);

  function serviceTile(provider) {
    const info = SERVICES[provider.provider] || {glyph: "•", tone: ""};
    const accounts = accountsFor(provider);
    const [state, label] = serviceState(provider, accounts);
    const chosen = chosenService === provider.provider;
    const tile = node("button", undefined, `svc${chosen ? " on" : ""}`);
    tile.type = "button";
    tile.setAttribute("aria-pressed", String(chosen));
    tile.addEventListener("click", () => { chosenService = provider.provider; renderConnections(); });
    const words = node("span", undefined, "svc-words");
    words.append(node("strong", provider.name), node("span", accounts.length ? accounts.map(shortIdentity).join(", ") : label, "faint"));
    tile.append(node("span", info.glyph, `glyph ${info.tone}`), words, node("span", label, `state ${state}`));
    return tile;
  }

  function permissionList(title, items, kind) {
    const block = node("div", undefined, "perm-block");
    block.append(node("h3", title, "label"));
    const list = node("ul", undefined, `perms ${kind}`);
    for (const item of items) list.append(node("li", item));
    block.append(list);
    return block;
  }

  function serviceDetail(provider) {
    const panel = $("service-detail");
    panel.replaceChildren();
    if (!provider) return;
    const info = SERVICES[provider.provider] || {glyph: "•", tone: "", reads: [], does: [], never: [], actions: "actions"};
    const accounts = accountsFor(provider);
    const [state, label] = serviceState(provider, accounts);
    const head = node("header", undefined, "svc-head");
    const words = node("div");
    words.append(node("h2", provider.name), node("p", accounts.length ? accounts.map(shortIdentity).join(", ") : label, "faint"));
    head.append(node("span", info.glyph, `glyph big ${info.tone}`), words);
    panel.append(head);
    if (provider.kind === "token") {
      tokenPanel(panel, provider, accounts, info);
      recentFor(panel, provider);
      return;
    }

    if (state === "off") {
      panel.append(node("p", "Connected services are turned off. Set INTEGRATIONS_ENABLED=true in .env and restart Bridge.", "notice-box"));
    } else if (state === "setup") {
      const key = provider.provider === "slack" ? "SLACK_CLIENT_ID" : provider.provider === "spotify" ? "SPOTIFY_CLIENT_ID" : "GOOGLE_CLIENT_ID";
      panel.append(node("p", `Add ${key} to .env and restart Bridge. Steps are in docs/SERVICES.md.`, "notice-box"));
    }
    const last = connectionData.activity.find((item) => item.provider === provider.provider);
    if (last && !last.ok) panel.append(node("p", `${last.event} ${ago(last.at)}: ${last.detail}`, "notice-box bad"));

    for (const account of accounts) {
      const writeScopes = new Set(provider.write_scopes);
      if (!account.scopes.some((scope) => writeScopes.has(scope))) {
        panel.append(node("p", `${shortIdentity(account)} is read-only. Reconnect with “Allow ${info.actions}” to let Bridge act after you approve.`, "notice-box"));
      }
    }

    panel.append(permissionList("Reads", info.reads, "reads"), permissionList("Does, after you approve", info.does, "does"), permissionList("Never", info.never, "never"));

    const pending = signIns[provider.provider];
    const controls = node("div", undefined, "svc-controls");
    if (pending) {
      const link = node("a", `Continue to ${provider.name} sign-in ↗`, "primary button-link");
      link.href = pending.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      controls.append(link, action("Cancel", () => { delete signIns[provider.provider]; renderConnections(); }, "ghost"));
      panel.append(controls, node("p", "Finish signing in in your browser. This updates by itself when you're done.", "hint"));
    } else if (state !== "off" && state !== "setup") {
      const allow = node("label", undefined, "allow");
      const box = node("input");
      box.type = "checkbox";
      box.checked = true;
      allow.append(box, node("span", `Allow ${info.actions}`));
      controls.append(action(accounts.length ? "Reconnect" : `Connect ${provider.name}`, () => startSignIn(provider, box.checked), accounts.length ? "secondary" : "primary"));
      for (const account of accounts) {
        controls.append(action("Disconnect", () => {
          if (!window.confirm(`Disconnect ${shortIdentity(account)} from Bridge?`)) return;
          execute(async () => {
            const result = await request("/api/v1/connections/disconnect", "POST", {account_id: account.account_id});
            await refreshConnections();
            notify(result.message);
          }, {refresh: false});
        }, "ghost danger"));
      }
      panel.append(allow, controls);
    }

    recentFor(panel, provider);
    const details = node("details", undefined, "tech");
    details.append(node("summary", "Exact permissions"),
      node("pre", `Callback: ${provider.redirect_uri}\n\nRead:\n${provider.read_scopes.join("\n")}\n\nActions:\n${provider.write_scopes.join("\n")}`));
    panel.append(details);
  }

  function recentFor(panel, provider) {
    const recent = connectionData.activity.filter((item) => item.provider === provider.provider).slice(0, 5);
    if (recent.length) {
      const block = node("div", undefined, "perm-block");
      block.append(node("h3", "Recently", "label"));
      const list = node("ul", undefined, "recent");
      for (const item of recent) {
        const row = node("li", undefined, item.ok ? "" : "bad");
        row.append(node("span", item.event), node("time", ago(item.at), "mono faint"));
        list.append(row);
      }
      block.append(list);
      panel.append(block);
    }
  }

  // Jira and GitHub connect with a token (or, for GitHub, the gh command line) instead of a
  // browser sign-in. The token goes straight to the Keychain; the page never shows it again.
  function tokenPanel(panel, provider, accounts, info) {
    const account = accounts[0];
    if (provider.error) panel.append(node("p", provider.error, "notice-box bad"));
    if (account && !account.scopes.includes("actions")) {
      panel.append(node("p", `Read-only. Connect again with “Allow ${info.actions}” to let Bridge act after you approve.`, "notice-box"));
    }
    panel.append(permissionList("Reads", info.reads, "reads"), permissionList("Does, after you approve", info.does, "does"), permissionList("Never", info.never, "never"));
    const form = node("form", undefined, "token-form");
    const field = (label, id, type, placeholder, value = "") => {
      const input = node("input");
      input.id = id;
      input.type = type;
      input.placeholder = placeholder;
      input.value = value;
      input.autocomplete = "off";
      input.spellcheck = false;
      const wrap = node("label", undefined, "token-field");
      wrap.append(node("span", label), input);
      form.append(wrap);
      return input;
    };
    const allow = node("label", undefined, "allow");
    const box = node("input");
    box.type = "checkbox";
    box.checked = account ? account.scopes.includes("actions") : true;
    allow.append(box, node("span", `Allow ${info.actions}`));
    const connect = (body) => execute(async () => {
      const result = await request("/api/v1/connections/token", "POST", {provider: provider.provider, allow_actions: box.checked, ...body});
      await refreshConnections();
      notify(`${provider.name} connected as ${result.identity}.${result.shared ? " Bridge can now answer questions about it." : ""}`);
    }, {refresh: false});
    const controls = node("div", undefined, "svc-controls");
    if (provider.provider === "jira") {
      if (account) panel.append(node("p", `Connected to ${account.site} as ${account.identity}.`, "hint"));
      const site = field("Your Atlassian site", "jira-site", "text", "yourteam.atlassian.net", account ? account.site : "");
      const email = field("Your Atlassian email", "jira-email", "email", "you@company.com");
      const token = field("API token", "jira-token", "password", "Paste the token");
      const create = node("a", "Create an API token ↗");
      create.href = "https://id.atlassian.com/manage-profile/security/api-tokens";
      create.target = "_blank";
      create.rel = "noopener noreferrer";
      const help = node("p", undefined, "hint");
      help.append(create, node("span", " — on Atlassian's site. Name it “Bridge”, copy it and paste it here."));
      form.append(help, allow);
      controls.append(action(account ? "Connect again" : "Connect Jira", () => {
        connect({site: site.value, email: email.value, token: token.value});
        token.value = "";
      }, account ? "secondary" : "primary"));
    } else {
      if (account) panel.append(node("p", `Connected as ${account.identity}${account.method === "cli" ? ", using your GitHub command line sign-in" : ""}.`, "hint"));
      if (provider.cli_available) {
        form.append(node("p", "You're signed in to the GitHub command line (gh) on this Mac, so Bridge can use that — nothing to paste.", "hint"));
      }
      const more = node("details", undefined, "token-more");
      more.append(node("summary", provider.cli_available ? "Or use a token instead" : "Use a token"));
      const tokenWrap = node("div");
      more.append(tokenWrap);
      const token = node("input");
      token.type = "password";
      token.placeholder = "Paste a GitHub token";
      token.autocomplete = "off";
      const make = node("a", "Create a fine-grained token ↗");
      make.href = "https://github.com/settings/personal-access-tokens/new";
      make.target = "_blank";
      make.rel = "noopener noreferrer";
      const help = node("p", undefined, "hint");
      help.append(make, node("span", " — give it read access to pull requests and issues (and write, to create issues and comments)."));
      tokenWrap.append(token, help, action("Connect with this token", () => { connect({token: token.value}); token.value = ""; }, "secondary"));
      if (!provider.cli_available) more.open = true;
      form.append(allow);
      if (provider.cli_available) {
        controls.append(action(account ? "Connect again with gh" : "Use my GitHub sign-in", () => connect({use_cli: true}), account ? "secondary" : "primary"));
      }
      form.append(more);
    }
    form.addEventListener("submit", (event) => event.preventDefault());
    if (account) {
      controls.append(action("Disconnect", () => {
        if (!window.confirm(`Disconnect ${provider.name} from Bridge?`)) return;
        execute(async () => {
          const result = await request("/api/v1/connections/disconnect", "POST", {account_id: provider.provider});
          await refreshConnections();
          notify(result.message);
        }, {refresh: false});
      }, "ghost danger"));
    }
    panel.append(form, controls);
  }

  function renderActivity() {
    const filter = $("activity-filter").value;
    const names = Object.fromEntries(connectionData.providers.map((item) => [item.provider, item.name]));
    const items = connectionData.activity.filter((item) =>
      !filter || (filter === "failed" ? !item.ok : item.provider === filter));
    const list = $("connection-activity");
    list.replaceChildren();
    if (!items.length) list.append(node("li", filter ? "Nothing here yet." : "No activity yet. Connect a service and ask Bridge something.", "line-empty"));
    for (const item of items.slice(0, 40)) {
      const row = node("li", undefined, `line${item.ok ? "" : " bad"}`);
      const info = SERVICES[item.provider] || {tone: ""};
      row.append(node("time", ago(item.at), "mono faint when-short"), node("span", names[item.provider] || item.provider, `src ${info.tone}`));
      const text = node("span", undefined, "line-main");
      text.append(node("span", item.event));
      if (item.detail) text.append(node("span", item.detail, item.ok ? "faint" : "line-error"));
      row.append(text);
      list.append(row);
    }
  }

  function renderConnections() {
    const providers = connectionData.providers;
    const connected = providers.filter((p) => accountsFor(p).length).length;
    $("connections-count").textContent = connectionData.enabled ? `${connected} of ${providers.length} connected` : "Turned off";
    $("connections-status").hidden = connectionData.enabled || !connectionData.message;
    $("connections-status").textContent = connectionData.enabled ? "" : connectionData.message || "";
    if (!providers.some((p) => p.provider === chosenService)) {
      chosenService = (providers.find((p) => accountsFor(p).length) || providers[0] || {}).provider || null;
    }
    $("service-grid").replaceChildren(...providers.map(serviceTile));
    serviceDetail(providers.find((p) => p.provider === chosenService));
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

  const MARK = '<svg viewBox="0 0 26 26" aria-hidden="true"><rect width="26" height="26" rx="7" fill="currentColor"/><path d="M6 18v-5M20 18v-5M4.5 13c4-5.5 13-5.5 17 0" fill="none" stroke="var(--accent-ink)" stroke-width="2" stroke-linecap="round"/></svg>';

  function stepList(steps) {
    const list = node("ol", undefined, "steps");
    for (const step of steps) {
      const state = step.success ? "done" : step.status === "confirmation_required" ? "waiting" : "failed";
      const item = node("li", undefined, state);
      item.append(node("span", state === "done" ? "✓" : state === "waiting" ? "◷" : "✕", "step-icon"), node("span", toolLabel(step.tool)));
      list.append(item);
    }
    return list;
  }

  function chatMessage(author, text, steps, meta = {}) {
    const empty = $("empty-chat");
    if (empty) empty.remove();
    const mine = author === "You";
    const card = node("article", undefined, `message ${mine ? "user" : "assistant"}`);
    if (meta.request_id) card.dataset.request = meta.request_id;
    if (mine) {
      card.append(node("p", text, "bubble"));
    } else {
      const avatar = node("span", undefined, "avatar");
      avatar.innerHTML = MARK;  // A fixed, local SVG; never user content.
      const body = node("div", undefined, "body");
      if (steps && steps.length) body.append(stepList(steps));
      body.append(node("p", text, "reply"));
      if (steps && steps.some((step) => "result" in step || "error" in step)) {
        const details = node("details", undefined, "tech");
        details.append(node("summary", "Technical details"), node("pre", JSON.stringify(steps, null, 2)));
        body.append(details);
      }
      card.append(avatar, body);
    }
    if (meta.at) {
      const time = node("time", new Date(meta.at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}));
      time.dateTime = meta.at;
      card.append(time);
    }
    $("conversation").append(card);
    scrollThread();
  }

  function scrollThread() {
    const thread = $("conversation");
    thread.scrollTop = thread.scrollHeight;
  }

  // The approval card: exactly what will happen, with Send / Don't send. ---------------------

  function describeAction(item) {
    const a = item.arguments || {};
    const list = (value) => (Array.isArray(value) ? value.join(", ") : String(value ?? ""));
    switch (item.action) {
      case "email_send": return {title: "Send this email?", go: "Send", fields: [["To", list(a.to)], ["Subject", a.subject]], body: a.body};
      case "email_create_draft": return {title: "Save this draft in Gmail?", go: "Save draft", fields: [["To", list(a.to)], ["Subject", a.subject]], body: a.body};
      case "slack_send_message": return {title: "Send this Slack message?", go: "Send", fields: [["To", a.channel]], body: a.text};
      case "messages_send": return {title: "Send this text?", go: "Send", fields: [["To", a.to]], body: a.text};
      case "focus_start": return {title: "Start focus?", go: "Start", fields: [["For", `${a.minutes} minutes`], ["On", a.task || "—"]]};
      default: {
        const fields = Object.entries(a)
          .filter(([key, value]) => key !== "account_id" && value !== null && value !== undefined && value !== "")
          .slice(0, 6)
          .map(([key, value]) => [key.replaceAll("_", " "), typeof value === "object" ? JSON.stringify(value) : String(value)]);
        return {title: item.message || `Allow ${toolLabel(item.action)}?`, go: "Approve", fields};
      }
    }
  }

  function approvalCard(item) {
    const info = describeAction(item);
    const card = node("section", undefined, "approval-card");
    card.setAttribute("aria-label", info.title);
    const head = node("div", undefined, "approval-head");
    head.append(node("h3", info.title), node("span", "needs your OK", "badge warm"));
    card.append(head);
    if (info.fields.length) {
      const fields = node("dl");
      for (const [label, value] of info.fields) fields.append(node("dt", label), node("dd", value));
      card.append(fields);
    }
    if (info.body) card.append(node("div", info.body, "approval-body"));
    const actions = node("div", undefined, "approval-actions");
    const go = node("button", undefined, "primary");
    go.type = "button";
    go.append(node("span", info.go), node("kbd", "⌘↵"));
    go.addEventListener("click", () => decide(true));
    const no = node("button", item.action.includes("send") ? "Don't send" : "Don't", "ghost");
    no.type = "button";
    no.addEventListener("click", () => decide(false));
    actions.append(go, no, node("span", "Nothing happens until you press it.", "faint note"));
    card.append(actions, node("p", "", "approval-error"));
    return card;
  }

  function showApproval() {
    document.querySelectorAll(".approval-card").forEach((card) => card.remove());
    if (!pending) return;
    const target = document.querySelector(`.message.assistant[data-request="${CSS.escape(pending.request_id || "")}"] .body`)
      || [...document.querySelectorAll(".message.assistant .body")].pop();
    if (!target) return;
    target.append(approvalCard(pending));
    scrollThread();
  }

  function approvalError(message) {
    const box = document.querySelector(".approval-card .approval-error");
    if (box) box.textContent = message;
  }

  function renderConversation(messages) {
    const first = messages.find((entry) => entry.role === "user");
    $("ask-title").textContent = first ? first.text.replace(/\s+/g, " ").slice(0, 70) : "New conversation";
    $("ask-when").textContent = first ? `Ask · started ${new Date(first.at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}` : "Ask";
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
    const [conversation, waiting] = await Promise.all([
      request("/api/v1/conversation"),
      request("/api/v1/approvals").catch(() => ({approvals: []})),
    ]);
    // An approval from this conversation (typed, spoken or scheduled) comes back after a reload.
    const asked = new Set(conversation.messages.map((entry) => entry.request_id));
    if (!pending) pending = waiting.approvals.find((item) => asked.has(item.request_id)) || null;
    else if (!waiting.approvals.some((item) => item.token === pending.token)) pending = null;
    renderConversation(conversation.messages);
    showApproval();
    syncControls();
  }

  function showResult(result) {
    // Chat requests are already rendered from the server's conversation log.
    const logged = loggedRequests.has(result.request_id);
    if (!logged) chatMessage("Bridge", result.message, result.steps);
    // Chat replies are already on screen; only repeat them in the banner when something failed.
    if (!logged || result.status === "failed") notify(result.message, result.status === "failed");
    if (result.status === "confirmation_required") {
      pending = {...result.confirmation, request_id: result.request_id, message: result.message};
      view("chat");
      showApproval();
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
      if (pending) approvalError(`${error.message} You can check Workflows for what already ran.`);
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
    if (phoneMode()) {
      await Promise.all([loadConversation(), refreshTasks()]);
      return;
    }
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
    $("pro-morning").checked = settings.morning_plan;
    $("pro-morning-time").value = settings.morning_time;
    $("pro-work-start").value = settings.work_start;
    $("pro-work-end").value = settings.work_end;
    const followups = $("followup-list");
    followups.replaceChildren();
    if (!data.followups.length) followups.append(node("p", "Nothing yet. Say “remind me if Olivier doesn't reply by Friday”.", "hint"));
    for (const item of data.followups) {
      const row = node("div", undefined, "method-row");
      const text = node("div");
      const due = new Date(item.due).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
      const state = {waiting: "waiting", overdue: "no reply yet", replied: "replied ✓"}[item.status] || item.status;
      text.append(node("strong", `${item.name} — ${item.about}`), node("span", `By ${due} · ${state}`, item.status === "overdue" ? "account-error" : "account-facts"));
      row.append(node("span", item.status === "replied" ? "✓" : "◷", "method-icon"), text);
      if (item.status !== "replied") row.append(action("Cancel", () => execute(async () => {
        await request("/api/v1/followups/cancel", "POST", {id: item.id});
        await refreshProactive();
      }, {refresh: false})));
      followups.append(row);
    }
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
        morning_plan: $("pro-morning").checked, morning_time: $("pro-morning-time").value || "08:30",
        work_start: $("pro-work-start").value || "09:00", work_end: $("pro-work-end").value || "18:00",
      });
      await refreshProactive();
      notify("Proactive settings saved.");
    }, {refresh: false});
  }
  for (const id of ["pro-meetings", "pro-lead", "pro-evening", "pro-evening-time", "pro-morning", "pro-morning-time", "pro-work-start", "pro-work-end"]) $(id).addEventListener("change", saveProactive);

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
    $("memory-count").textContent = data.memories.length ? `${data.memories.length} thing${data.memories.length === 1 ? "" : "s"}` : "Nothing yet";
    if (!data.memories.length) list.append(node("li", "Nothing yet. Add something above, or say “remember that …” anywhere.", "line-empty"));
    for (const fact of [...data.memories].reverse()) {
      const row = node("li", undefined, "fact");
      row.append(node("span", fact.text, "fact-text"), node("time", ago(fact.created_at), "mono faint"),
        action("Forget", () => execute(async () => {
          await request("/api/v1/memories/forget", "POST", {id: fact.id});
          await refreshMemories();
        }, {refresh: false}), "ghost small"));
      list.append(row);
    }
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
    $("provider-summary").textContent = `On this Mac · ${name}`;
    let policy;
    if (provider.remote_tool_results === "all") {
      policy = "Messages and all tool results are sent to OpenAI.";
    } else if (provider.remote_tool_results === "allowlist") {
      policy = "Messages go to OpenAI. OpenAI can read results from the tools listed below; other results share only whether they succeeded.";
    } else {
      policy = "Messages go to OpenAI. Tool results share only whether they succeeded; their contents stay on this Mac.";
    }
    $("provider-privacy-hint").textContent = "Requests go to OpenAI. Anything that sends or changes something waits for your OK.";
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

  // Inside the Bridge app the window signs itself in with a one-time link from the app:
  // an expired session never needs a password or Google there (Google sign-in can't
  // finish inside an app window anyway; it would end up signed in in the browser).
  const inApp = Boolean(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.bridge);
  let autoSignInFailed = false;

  function showPhoneAuth() {
    signedIn = false;
    document.body.classList.add("signed-out", "phone");
    $("workspace").hidden = true;
    $("tabbar").hidden = true;
    $("auth-panel").hidden = false;
    $("auth-loading").hidden = true;
    for (const id of ["signup-form", "login-form", "auth-need-menu"]) $(id).hidden = true;
    $("phone-login").hidden = false;
    const faceId = authStatus.methods.passkey && window.PublicKeyCredential;
    $("phone-faceid").hidden = !faceId;
    $("phone-login-title").textContent = faceId ? "Welcome back" : "Sign in from your Mac";
    $("phone-login-hint").textContent = faceId
      ? "Your Mac needs to be on, with Bridge running."
      : "On your Mac, open Bridge › Settings › Phone and scan the QR code with this phone's camera.";
  }

  async function showAuth() {
    authStatus = await request("/api/v1/auth/status");
    if (authStatus.signed_in) return enterWorkspace();
    if (authStatus.phone) return showPhoneAuth();
    if (inApp && authStatus.has_owner && !autoSignInFailed) {
      signedIn = false;
      $("workspace").hidden = true;
      $("auth-panel").hidden = false;
      $("signup-form").hidden = true;
      $("login-form").hidden = true;
      $("auth-need-menu").hidden = true;
      $("auth-loading").hidden = false;
      $("auth-loading").textContent = "Signing you in…";
      window.webkit.messageHandlers.bridge.postMessage({action: "signin"});
      return;
    }
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
    $("disconnect").hidden = inApp;  // In the app, signing out would just sign back in.
    $("account-chip").hidden = false;
    const owner = status.owner || {name: "", email: ""};
    $("account-name").textContent = owner.name || owner.email;
    $("account-initials").textContent = (owner.name || owner.email || "?").trim().split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
    $("connection-status").textContent = "Signed in";
    $("connection-status").classList.add("online");
    syncControls();
    if (status.phone) {
      document.body.classList.add("phone");
      $("phone-login").hidden = true;
      $("tabbar").hidden = false;
      await refreshAll();
      view("today");
      if (window.BridgePhone) window.BridgePhone.signedIn();
      return;
    }
    await refreshAll();
    // First launch (or setup never finished): the guided setup; otherwise Today.
    if (window.BridgeOnboarding) await window.BridgeOnboarding.startIfNeeded();
    else view("today");
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

  $("phone-faceid").addEventListener("click", () => $("passkey-login").click());
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
  $("account-chip").addEventListener("click", () => view("settings", "account"));

  // ---- Start-up: menu-bar link, provider return, or existing session ----------------

  async function handleHash() {
    const hash = new URLSearchParams(window.location.hash.slice(1));
    if ([...hash.keys()].length) history.replaceState(null, "", window.location.pathname);
    if (hash.get("auth-error")) authError(hash.get("auth-error"));
    // The app already tried to sign this window in; if that failed, show the normal login.
    if (hash.get("auto")) autoSignInFailed = true;
    const pair = hash.get("pair");
    if (pair) {
      const response = await fetch("/api/v1/phone/pair", {method: "POST", credentials: "same-origin",
        cache: "no-store", redirect: "error", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ticket: pair})}).catch(() => null);
      if (!response || !response.ok) {
        const data = response ? await response.json().catch(() => ({})) : {};
        authError(typeof data.detail === "string" ? data.detail : "That code didn't work. Show a new one on your Mac.");
      } else {
        sessionStorage.setItem("bridge-just-paired", "1");
      }
    }
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
    // Opened from a notification (or the panel) about a particular screen.
    const go = hash.get("view");
    if (go && signedIn && /^[a-z]+$/.test(go) && $(`view-${go}`)) view(go);
    if (hash.get("account-linked") && signedIn) {
      view("settings", "account");
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

  $("message").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("message-form").requestSubmit();
    }
  });
  $("message").addEventListener("input", () => {
    $("message").style.height = "auto";
    $("message").style.height = `${Math.min($("message").scrollHeight, 200)}px`;
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
      showApproval();
      const result = await watchTask(progress);
      await loadConversation();
      return result;
    });
  }
  // A card elsewhere (Today) approves an action by its token; the result lands in Ask.
  function decideToken(token, approved) {
    if (busy || pending) return Promise.resolve();
    return execute(async () => {
      const progress = await request("/api/v1/tasks/confirm", "POST", {token, approved});
      const result = await watchTask(progress);
      await loadConversation();
      return result;
    }, {refresh: false});
  }

  function ask(text) {
    view("chat");
    $("message").value = text;
    $("message-form").requestSubmit();
  }

  function focusAsk() {
    view("chat");
    $("message").focus();
  }
  $("ask-shortcut").addEventListener("click", focusAsk);
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k" && signedIn) {
      event.preventDefault();
      focusAsk();
    }
  });

  // The Mac app tells the page about the microphone ("Hey Bridge"); Settings shows it.
  window.BridgeNative = {
    state: null,
    voice(status) {
      this.state = status;
      document.dispatchEvent(new CustomEvent("bridge-voice", {detail: status}));
    },
    send(message) {
      try { window.webkit.messageHandlers.bridge.postMessage(message); } catch { /* in a browser */ }
    },
  };

  // What other screens (today.js) may use. Same page, same session; nothing new is exposed.
  window.BridgeUI = {
    request, view, notify, ask, decideToken,
    refreshAccount: () => execute(refreshAccount, {refresh: false}),
    run: (task) => execute(task, {refresh: false}),
    owner: () => (authStatus && authStatus.owner) || {name: "", email: ""},
    phone: phoneMode,
    passkeyJSON: credentialJSON,
    fromB64,
    busy: () => busy || Boolean(pending),
  };

  document.addEventListener("keydown", (event) => {
    if (pending && (event.metaKey || event.ctrlKey) && event.key === "Enter" && !$("view-chat").hidden) {
      event.preventDefault();
      decide(true);
    }
  });
  // Recent chats: kept on this Mac for the days chosen in Settings › Privacy.
  function closeRecent() {
    $("recent-chats").hidden = true;
    $("ask-history").setAttribute("aria-expanded", "false");
  }

  async function showRecent() {
    const {chats, current, days} = await request("/api/v1/chats");
    const list = $("recent-list");
    list.replaceChildren();
    for (const chat of chats) {
      const item = node("li", undefined, chat.id === current ? "recent-item current" : "recent-item");
      const open = node("button", undefined, "recent-open");
      open.type = "button";
      open.append(node("span", chat.title, "recent-title"), node("span", chat.id === current ? "Open now" : ago(chat.updated), "recent-when faint"));
      open.addEventListener("click", () => execute(async () => {
        closeRecent();
        if (chat.id !== current) await request("/api/v1/chats/open", "POST", {id: chat.id});
        pending = null;
        await loadConversation();
      }, {refresh: false}));
      const remove = node("button", "×", "chip-x");
      remove.type = "button";
      remove.setAttribute("aria-label", `Delete “${chat.title}”`);
      remove.addEventListener("click", () => execute(async () => {
        await request("/api/v1/chats/delete", "POST", {id: chat.id});
        if (chat.id === current) await loadConversation();
        await showRecent();
      }, {refresh: false}));
      item.append(open, remove);
      list.append(item);
    }
    $("recent-note").textContent = !days ? "Chats aren't kept: each one is forgotten when Bridge quits."
      : chats.length ? `Kept on this Mac for ${days} days, then deleted.`
      : `No chats yet. They're kept on this Mac for ${days} days.`;
    $("recent-chats").hidden = false;
    $("ask-history").setAttribute("aria-expanded", "true");
  }

  $("ask-history").addEventListener("click", () => {
    if (!$("recent-chats").hidden) return closeRecent();
    showRecent().catch((error) => notify(error.message, true));
  });
  $("recent-settings").addEventListener("click", () => { closeRecent(); view("settings", "privacy"); });
  $("recent-activity").addEventListener("click", () => { closeRecent(); view("workflows"); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("recent-chats").hidden) closeRecent(); });
  document.addEventListener("click", (event) => {
    if (!$("recent-chats").hidden && !event.target.closest("#recent-chats, #ask-history")) closeRecent();
  });

  // Which window "this" means: app and title only, refreshed when you come to Ask.
  async function peekScreen() {
    try {
      const {window: seen} = await request("/api/v1/screen/peek");
      $("screen-chip").hidden = !seen;
      if (seen) {
        $("screen-chip-text").textContent = seen.private ? `${seen.app} is private — Bridge won't read it`
          : `Can see: ${seen.app}${seen.window ? ` — ${seen.window}` : ""}`;
      }
    } catch {
      $("screen-chip").hidden = true;
    }
  }
  window.addEventListener("focus", () => { if (signedIn && !$("view-chat").hidden) peekScreen(); });

  $("reset-chat").addEventListener("click", () => execute(async () => {
    await request("/api/v1/conversation/reset", "POST");
    closeRecent();
    renderConversation([]);
    notify("Started a new conversation.");
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
