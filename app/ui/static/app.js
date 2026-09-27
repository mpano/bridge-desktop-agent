"use strict";

// Credentials and approval tokens exist only in this closure, never browser storage.
(() => {
  const $ = (id) => document.getElementById(id);
  let token = "";
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
    $("workspace").disabled = busy || !token || Boolean(pending);
    $("connect-form").querySelector("button").disabled = busy;
    $("disconnect").disabled = busy || Boolean(pending);
    $("approve").disabled = busy;
    $("decline").disabled = busy;
    $("review-workflows").disabled = busy;
    $("busy-status").hidden = !busy;
    $("workspace").setAttribute("aria-busy", String(busy));
  }

  async function request(path, method = "GET", body) {
    const headers = {Authorization: `Bearer ${token}`};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {method, headers, credentials: "omit", cache: "no-store",
        redirect: "error", body: body === undefined ? undefined : JSON.stringify(body)});
    } catch {
      throw new Error("Connection lost. If an action was submitted, review its workflow before retrying.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail :
        `Request rejected (${response.status}). Check your input and connection.`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function view(name) {
    document.querySelectorAll(".view").forEach((panel) => { panel.hidden = panel.id !== `view-${name}`; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      if (button.dataset.view === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  function chatMessage(author, text, steps) {
    const empty = $("empty-chat");
    if (empty) empty.remove();
    const card = node("article", undefined, `message ${author === "You" ? "user" : "assistant"}`);
    card.append(node("div", author.toUpperCase(), "author"), node("p", text));
    if (steps && steps.length) {
      const details = node("details");
      details.append(node("summary", `${steps.length} execution step(s)`),
        node("pre", JSON.stringify(steps, null, 2)));
      card.append(details);
    }
    $("conversation").append(card);
    $("conversation").scrollTop = $("conversation").scrollHeight;
  }

  function showResult(result) {
    chatMessage("Desktop Agent", result.message, result.steps);
    notify(result.message, result.status === "failed");
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
      if (refresh && token) await refreshAll();
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
    if (["failed", "interrupted", "paused", "confirmation_required"].includes(status)) badge.classList.add(status);
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
      refreshCapabilities(), refreshDiagnostics()
    ]);
    renderProjects(projects.projects);
    $("default-browser").value = preferences.default_browser;
    $("default-editor").value = preferences.default_editor;
  }

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
      card.append(node("h3", check.name), node("span", check.status, "status"), node("p", check.message));
      $("diagnostics-list").append(card);
    }
  }

  function renderProvider(provider) {
    const local = provider.name === "ollama";
    const name = local ? "Local Ollama" : "OpenAI (remote)";
    $("provider-summary").textContent = `${name} handles language requests.`;
    let policy;
    if (local) {
      policy = "Messages and tool results are sent to your local Ollama server. No OpenAI fallback is used.";
    } else if (provider.remote_tool_results === "all") {
      policy = "Messages and all tool results are sent to OpenAI.";
    } else if (provider.remote_tool_results === "allowlist") {
      policy = "Messages go to OpenAI. Tool-result contents are shared only for allowed tools; other results share execution status.";
    } else {
      policy = "Messages go to OpenAI. Tool results share execution status only; their contents are withheld.";
    }
    $("provider-privacy-hint").textContent = `${policy} Screenshot images are not uploaded.`;
    const panel = $("provider-details");
    panel.replaceChildren(node("h3", name), node("p", `Model: ${provider.model}`), node("p", policy));
    panel.append(node("p", `Remote tool-result policy: ${provider.remote_tool_results}${local ? " (inactive in local mode)" : ""}`));
    if (provider.remote_tool_results === "allowlist") {
      panel.append(node("p", `Allowed tools: ${provider.remote_tool_result_allowlist.join(", ") || "none"}`));
    }
    panel.append(node("p", "This policy does not redact your messages. Avoid including secrets in requests.", "hint"));
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

  $("connect-form").addEventListener("submit", (event) => {
    event.preventDefault();
    execute(async () => {
      token = $("api-token").value.trim();
      $("api-token").value = "";
      try {
        await refreshAll();
      } catch (error) {
        token = "";
        throw error;
      }
      $("connect-panel").hidden = true;
      $("workspace").hidden = false;
      $("disconnect").hidden = false;
      $("connection-status").textContent = "Connected locally";
      $("message").focus();
    }, {refresh: false});
  });

  $("disconnect").addEventListener("click", () => {
    if (busy || pending) return;
    token = "";
    // Reload clears rendered local data and all in-memory conversation/approval UI state.
    window.location.reload();
  });

  $("message-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const message = $("message").value.trim();
    if (!message || busy || pending) return;
    $("message").value = "";
    chatMessage("You", message);
    execute(async () => watchTask(await request("/api/v1/tasks", "POST", {message})));
  });

  async function watchTask(progress) {
      liveTask = progress.request_id;
      $("live-task").hidden = false;
      $("stop-task").disabled = false;
      try {
        while (!progress.result) {
          $("task-progress").textContent = `${progress.status}: ${progress.completed_steps} recorded steps. ` +
            (progress.current_tool ? `Running ${progress.current_tool}. ` : "") +
            (progress.remaining_tools.length ? `Next: ${progress.remaining_tools.join(", ")}. ` : "") +
            (progress.cancel_requested ? "Stopping after the current operation." : "");
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
      return await watchTask(progress);
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
    $("conversation").replaceChildren();
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
  document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => {
    $("message").value = button.dataset.example;
    $("message").focus();
  }));
})();
