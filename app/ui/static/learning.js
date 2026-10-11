"use strict";

// Settings › Learning: what Bridge noticed this week, lessons it proposes (tested on your past
// cases), the lessons in use, and the switch.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  const {node, button} = window.BridgeDOM;

  async function show() {
    try { render(await ui().request("/api/v1/learning")); } catch (error) { ui().notify(error.message, true); }
  }

  async function act(path, body, message) {
    try {
      await ui().request(path, "POST", body);
      if (message) ui().notify(message);
    } catch (error) {
      ui().notify(error.message, true);
    }
    show();
  }

  function tested(t) {
    if (!t || !t.method || t.method === "untested") return "Not tested yet.";
    if (t.method === "replayed" && t.before !== undefined) return `Re-checked on ${t.cases} past cases: ${t.after} right with it, ${t.before} without.`;
    if (t.method === "replayed") return `Tried on ${t.cases} drafts you rewrote: closer to what you sent in ${t.better}${t.worse ? `, further in ${t.worse}` : ""}.`;
    return `Judged against ${t.cases} past cases: would have helped in ${t.better}.`;
  }

  const row = (name, hint, ...controls) => {
    const line = node("div", undefined, "set");
    const words = node("div", undefined, "set-words");
    words.append(node("span", name, "set-name"));
    if (hint) words.append(node("span", hint, "set-hint"));
    const end = node("span", undefined, "set-end");
    end.append(...controls.filter(Boolean));
    line.append(words, end);
    return line;
  };

  function render(data) {
    // This week.
    const week = $("learning-week");
    week.replaceChildren(node("p", "This week", "label"));
    const grid = node("div", undefined, "learn-week");
    for (const line of data.week.lines) {
      const cell = node("div", undefined, "learn-stat");
      const change = line.this_week - line.last_week;
      cell.append(node("strong", String(line.this_week)), node("span", line.label, "muted"),
        node("span", line.last_week || line.this_week ? `${change > 0 ? "+" : ""}${change} vs last week` : "—",
          `faint ${change < 0 ? "good-text" : ""}`));
      grid.append(cell);
    }
    week.append(grid);
    if (data.week.learned.length) {
      week.append(node("p", `Learned this week: ${data.week.learned.map((r) => r.rule).join(" · ")}`, "hint"));
    }

    // Proposed lessons.
    const proposed = $("learning-proposed");
    proposed.replaceChildren();
    if (data.proposed.length) {
      const card = node("div", undefined, "card group learn-proposals");
      card.append(node("p", "New lessons to review", "label attention"));
      for (const rule of data.proposed) {
        card.append(row(rule.rule, `${rule.area_name} · ${rule.why} ${tested(rule.tested)}`,
          button("Use it", "primary small", () => act("/api/v1/learning/decide", {id: rule.id, status: "active"}, "Lesson in use.")),
          button("No", "ghost small", () => act("/api/v1/learning/decide", {id: rule.id, status: "rejected"}))));
      }
      proposed.append(card);
    }

    // In use.
    const active = $("learning-active");
    active.replaceChildren(node("p", "Lessons in use", "label learn-head"));
    if (!data.active.length) active.append(node("p", "None yet. Bridge proposes lessons on Fridays, or when you ask below.", "hint learn-pad"));
    for (const rule of data.active) {
      const on = rule.status === "active";
      active.append(row(rule.rule, `${rule.area_name}${on ? "" : " · off"}`,
        button(on ? "Turn off" : "Turn on", "secondary small", () => act("/api/v1/learning/decide", {id: rule.id, status: on ? "off" : "active"}))));
    }

    // Settings.
    const toggle = node("button", undefined, data.enabled ? "switch on" : "switch");
    toggle.type = "button";
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", String(data.enabled));
    toggle.setAttribute("aria-label", "Learn from my corrections");
    toggle.append(node("span", undefined, "knob"));
    toggle.addEventListener("click", () => act("/api/v1/settings/app", {learning_enabled: !data.enabled}));
    const look = button(data.waiting ? `Look for lessons now (${data.waiting} new)` : "Look for lessons now", "secondary small", async () => {
      look.disabled = true;
      look.textContent = "Looking…";
      try {
        const result = await ui().request("/api/v1/learning/propose", "POST");
        ui().notify(result.proposed ? `${result.proposed} new lesson${result.proposed === 1 ? "" : "s"} to review.` : "Nothing new to learn yet.");
      } catch (error) { ui().notify(error.message, true); }
      show();
    });
    look.disabled = !data.enabled;
    $("learning-settings").replaceChildren(
      row("Learn from my corrections", data.enabled ? "On. Corrections stay on this Mac for 60 days." : "Off: nothing is noted.", toggle),
      row("Find lessons", "Bridge also does this every Friday afternoon and tells you.", look),
      row("Forget everything learned", "Deletes the corrections and every lesson.", button("Forget", "ghost danger small", () => {
        if (window.confirm("Forget all corrections and lessons?")) act("/api/v1/learning/forget", {}, "Forgotten.");
      })),
    );
  }

  window.BridgeLearning = {show};
})();
