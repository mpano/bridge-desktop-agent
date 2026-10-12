"use strict";

// Recipes: "when X, do Y". In Automations you describe one in plain words, check exactly what
// it will do, and create it; each recipe asks first or runs automatically. On Today (and the
// phone) runs waiting for your OK show the exact actions with Approve and Skip.
(() => {
  const {button, node, link} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let data = null;
  let proposal = null;  // A plan shown for review, not saved yet.
  let planning = false;


  function ago(seconds) {
    const when = new Date(seconds * 1000);
    const minutes = Math.round((Date.now() - when) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    return when.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
  }

  async function refresh() {
    try {
      data = await ui().request("/api/v1/recipes");
    } catch {
      data = null;
    }
    renderManager();
    renderWaiting();
  }

  async function act(path, body, message) {
    try {
      const result = await ui().request(path, "POST", body);
      if (message) ui().notify(typeof message === "function" ? message(result) : message);
    } catch (error) {
      ui().notify(error.message, true);
    }
    await refresh();
  }

  // Automations ------------------------------------------------------------------------------

  function steps(list) {
    const ol = node("ol", undefined, "recipe-steps");
    for (const step of list) ol.append(node("li", step));
    return ol;
  }

  function renderProposal() {
    const box = $("recipe-proposal");
    box.replaceChildren();
    box.hidden = !proposal;
    if (!proposal) return;
    box.append(node("p", "Check it before it's saved", "label"));
    const when = node("p", undefined, "recipe-when");
    when.append(node("strong", "When "), document.createTextNode(proposal.when));
    box.append(node("h3", proposal.plan.name), when, node("p", "Then", "recipe-then"), steps(proposal.steps));
    box.append(node("p", "Shown with an example. Each run asks you first; you can make it automatic later.", "hint"));
    const row = node("div", undefined, "row-actions");
    row.append(
      button("Create recipe", "primary small", async () => {
        const plan = proposal.plan;
        proposal = null;
        renderProposal();
        await act("/api/v1/recipes", {plan}, (made) => `Recipe ready: ${made.name}`);
      }),
      button("Cancel", "ghost small", () => {
        proposal = null;
        renderProposal();
      }),
    );
    box.append(row);
  }

  function modeSwitch(recipe) {
    const group = node("div", undefined, "seg small");
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", `How ${recipe.name} runs`);
    for (const [key, label] of [["ask", "Asks first"], ["auto", "Automatic"]]) {
      const option = button(label, recipe.mode === key ? "on" : "", () => {
        if (recipe.mode === key) return;
        if (key === "auto" && !confirm(`Run “${recipe.name}” automatically? It will do this without asking:\n\n• ${recipe.steps.join("\n• ")}`)) return;
        act("/api/v1/recipes/update", {id: recipe.id, mode: key}, key === "auto" ? "It runs automatically now." : "It asks you first now.");
      });
      option.setAttribute("role", "radio");
      option.setAttribute("aria-checked", String(recipe.mode === key));
      group.append(option);
    }
    return group;
  }

  function renderManager() {
    const list = $("recipes");
    if (!list) return;
    list.replaceChildren();
    if (!data) return;
    for (const recipe of data.recipes) {
      const item = node("li", undefined, `recipe${recipe.enabled ? "" : " paused"}`);
      const head = node("div", undefined, "recipe-head");
      head.append(node("h3", recipe.name));
      if (recipe.mode === "auto") head.append(node("span", "Automatic", "pill pending"));
      if (!recipe.enabled) head.append(node("span", "Paused", "mono faint"));
      const when = node("p", undefined, "recipe-when");
      when.append(node("strong", "When "), document.createTextNode(recipe.when));
      item.append(head, when, steps(recipe.steps));
      if (recipe.problem) item.append(node("p", recipe.problem, "line-error"));
      const foot = node("div", undefined, "recipe-foot");
      const ran = recipe.runs ? `Ran ${recipe.runs} time${recipe.runs === 1 ? "" : "s"} · last ${ago(recipe.last_run)}${recipe.last_status === "failed" ? " (didn't finish)" : ""}` : "Hasn't run yet";
      foot.append(node("span", ran, "faint"), modeSwitch(recipe),
        button(recipe.enabled ? "Pause" : "Resume", "ghost small", () => act("/api/v1/recipes/update", {id: recipe.id, enabled: !recipe.enabled})),
        button("Delete", "ghost small", () => {
          if (confirm(`Delete “${recipe.name}”?`)) act("/api/v1/recipes/delete", {id: recipe.id}, "Recipe deleted.");
        }));
      item.append(foot);
      list.append(item);
    }
    if (!data.recipes.length) list.append(node("li", "No recipes yet. Describe one above, or tap an example.", "line-empty"));

    const recent = $("recipe-recent");
    recent.replaceChildren();
    recent.hidden = !data.recent.length;
    if (data.recent.length) {
      recent.append(node("p", "Recent runs", "label"));
      for (const run of data.recent.slice(0, 6)) {
        const row = node("div", undefined, `recipe-run ${run.status}`);
        const mark = {done: "✓", failed: "✕", skipped: "–", expired: "–", running: "…"}[run.status] || "";
        row.append(node("span", mark, "recipe-mark"), node("span", `${run.recipe}: ${run.event.headline || ""}`, "grow"),
          node("span", run.status === "failed" ? run.result || "Didn't finish" : run.status, "faint"), node("span", ago(run.decided || run.created), "mono faint"));
        recent.append(row);
      }
    }

  }

  // From the one box in Automations: work out the recipe and show it before it's saved.
  async function propose(text) {
    if (planning) return false;
    planning = true;
    try {
      proposal = await ui().request("/api/v1/recipes/plan", "POST", {text});
      renderProposal();
      $("recipe-proposal").scrollIntoView({behavior: "smooth", block: "center"});
      return true;
    } catch (error) {
      ui().notify(error.message, true);
      return false;
    } finally {
      planning = false;
    }
  }

  // Today: runs waiting for your OK ---------------------------------------------------------

  function renderWaiting() {
    const card = $("recipe-waiting");
    if (!card) return;
    const waiting = (data && data.waiting) || [];
    card.hidden = !waiting.length;
    const list = $("recipe-waiting-list");
    list.replaceChildren();
    $("recipe-waiting-count").textContent = waiting.length > 1 ? String(waiting.length) : "";
    for (const run of waiting) {
      const item = node("li", undefined, "recipe-ask");
      const top = node("div", undefined, "recipe-ask-top");
      top.append(node("strong", run.recipe), node("span", ago(run.created), "mono faint"));
      const what = node("p", undefined, "recipe-event");
      if (run.event.url) what.append(link(run.event.headline || "What happened", run.event.url));
      else what.append(document.createTextNode(run.event.headline || ""));
      if (run.event.detail) what.append(node("span", ` · ${run.event.detail}`, "faint"));
      const row = node("div", undefined, "row-actions");
      row.append(
        button(run.steps.length > 1 ? "Approve all" : "Approve", "primary small", () => act("/api/v1/recipes/runs/decide", {id: run.id, approve: true},
          (result) => (result.status === "done" ? "Done." : `Didn't finish: ${result.result}`))),
        button("Skip", "ghost small", () => act("/api/v1/recipes/runs/decide", {id: run.id, approve: false})),
      );
      item.append(top, what, steps(run.steps), row);
      list.append(item);
    }
  }

  window.BridgeRecipes = {refresh, propose};
})();
