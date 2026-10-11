"use strict";

// Settings › Usage & models: this month's AI spending (by feature, model and day), which model
// does which job, and the monthly budget.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  const {node, button} = window.BridgeDOM;

  const money = (value) => (value < 0.01 && value > 0 ? "<$0.01" : `$${value.toFixed(2)}`);

  async function show() {
    try { render(await ui().request("/api/v1/usage")); } catch (error) { ui().notify(error.message, true); }
  }

  async function save(body, message) {
    try {
      render(await ui().request("/api/v1/usage/settings", "POST", body));
      if (message) ui().notify(message);
    } catch (error) {
      ui().notify(error.message, true);
      show();
    }
  }

  function bars(title, items, labelOf, valueOf, noteOf) {
    const card = node("div", undefined, "card usage-list");
    card.append(node("p", title, "label"));
    const top = Math.max(...items.map(valueOf), 0.000001);
    if (!items.length) card.append(node("p", "Nothing yet this month.", "hint"));
    for (const item of items.slice(0, 8)) {
      const row = node("div", undefined, "usage-row");
      const words = node("div", undefined, "usage-words");
      words.append(node("span", labelOf(item)), node("span", money(valueOf(item)), "mono"));
      const track = node("div", undefined, "usage-track");
      const fill = node("div", undefined, "usage-fill");
      fill.style.width = `${Math.max(2, (valueOf(item) / top) * 100)}%`;
      track.append(fill);
      row.append(words, track);
      if (noteOf) row.append(node("span", noteOf(item), "faint usage-note"));
      card.append(row);
    }
    return card;
  }

  function render(data) {
    // This month.
    const total = $("usage-total");
    total.replaceChildren();
    const head = node("div", undefined, "usage-head");
    head.append(node("strong", money(data.spent), "usage-big"),
      node("span", `this month · on track for about ${money(data.projected)}${data.budget ? ` · budget $${data.budget}` : ""}`, "muted"));
    total.append(head);
    if (data.budget) {
      const track = node("div", undefined, "usage-track big");
      const fill = node("div", undefined, `usage-fill${data.spent >= data.budget ? " over" : data.spent >= data.budget * 0.8 ? " near" : ""}`);
      fill.style.width = `${Math.min(100, (data.spent / data.budget) * 100)}%`;
      track.append(fill);
      total.append(track);
    }
    if (data.saving) total.append(node("p", "Over budget: Bridge is using the fast model and has paused background checks until next month.", "notice-box"));
    if (data.unpriced) total.append(node("p", "Some calls used a model without a known price, so they're counted as $0.", "hint"));
    const days = node("div", undefined, "usage-days");
    const peak = Math.max(...data.days.map((d) => d.cost), 0.000001);
    for (const day of data.days) {
      const bar = node("span", undefined, "usage-day");
      bar.style.height = `${Math.max(3, (day.cost / peak) * 48)}px`;
      bar.title = `${day.day}: ${money(day.cost)}`;
      days.append(bar);
    }
    if (data.days.length) total.append(days);

    // By feature and model.
    $("usage-breakdown").replaceChildren(
      bars("By feature", data.tasks, (t) => t.label, (t) => t.cost, (t) => `${t.calls} calls`),
      bars("By model", data.models, (m) => m.model, (m) => m.cost, (m) => `${(m.tokens / 1000).toFixed(0)}k tokens`),
    );

    // Models.
    const picker = (label, current, key) => {
      const select = node("select", undefined, "small-select");
      select.setAttribute("aria-label", label);
      const names = data.choices.map((c) => c.model);
      if (!names.includes(current)) data.choices.unshift({model: current, input: null, output: null});
      for (const choice of data.choices) {
        const price = choice.input === null ? "" : ` · $${choice.input} / $${choice.output}`;
        const option = node("option", `${choice.model}${price}`);
        option.value = choice.model;
        option.selected = choice.model === current;
        select.append(option);
      }
      select.addEventListener("change", () => save({[key]: select.value}, `Using ${select.value} from now on.`));
      return select;
    };
    const jobs = (tier) => data.routing.filter((r) => r.tier === tier).map((r) => r.label.toLowerCase()).join(", ");
    const row = (name, hint, control) => {
      const line = node("div", undefined, "set");
      const words = node("div", undefined, "set-words");
      words.append(node("span", name, "set-name"), node("span", hint, "set-hint"));
      line.append(words);
      if (control) line.append(control);
      return line;
    };
    $("usage-models").replaceChildren(
      row("Smart model", `For ${jobs("smart")}.`, picker("Smart model", data.smart, "smart")),
      row("Fast model", `For ${jobs("fast")}.`, picker("Fast model", data.fast, "fast")),
      row("Prices", "Per million tokens: what you send / what comes back. Bridge tries a model before switching to it.", null),
    );

    // Budget.
    const amount = node("input");
    amount.type = "number";
    amount.min = "0";
    amount.step = "1";
    amount.placeholder = "No budget";
    amount.value = data.budget ? String(data.budget) : "";
    amount.className = "usage-amount";
    amount.setAttribute("aria-label", "Monthly budget in dollars");
    const set = button("Save", "secondary small", () => save({budget: Number(amount.value || 0)}, amount.value ? `Budget set to $${amount.value} a month.` : "Budget removed."));
    const budgetEnd = node("span", undefined, "set-end");
    budgetEnd.append(node("span", "$", "faint"), amount, set);
    const mode = node("div", undefined, "seg");
    for (const [key, text] of [["warn", "Just tell me"], ["save", "Save money"]]) {
      const option = button(text, key === data.budget_mode ? "on" : "", () => save({mode: key}));
      option.setAttribute("aria-pressed", String(key === data.budget_mode));
      mode.append(option);
    }
    $("usage-budget").replaceChildren(
      row("Monthly budget", "Bridge tells you at 80% and 100%.", budgetEnd),
      row("When it's reached", data.budget_mode === "save" ? "Bridge switches everything to the fast model and pauses background checks until next month." : "Bridge keeps going and just lets you know.", mode),
    );
  }

  window.BridgeUsage = {show};
})();
