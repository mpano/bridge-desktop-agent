"use strict";

// People: who you work with (search, list), a profile for each (working on together, what's
// open both ways, meetings, messages, reviews, your note, merge, forget), and on Today the
// brief before your next meeting with people Bridge knows.
(() => {
  const {button, node, link} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let list = [];
  let query = "";
  let open = null;
  let noteTimer = null;
  let togetherBox = null;  // "Working on together", filled in when the summary arrives.

  const COLORS = [["#6f7dff", "#b48cff"], ["#2fa58a", "#7fd1a7"], ["#f06a5b", "#ffb08a"], ["#2b8bc6", "#7cc7ee"], ["#c26a9b", "#f0a8c8"], ["#e0a21a", "#ffe07a"], ["#8a4fe0", "#e07ad8"]];
  function face(name, size = 36) {
    const letters = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
    let hash = 0;
    for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    const [a, b] = COLORS[hash % COLORS.length];
    const box = node("span", letters, "pp-face");
    box.style.background = `linear-gradient(135deg, ${a}, ${b})`;
    box.style.width = box.style.height = `${size}px`;
    box.style.fontSize = `${Math.round(size * 0.38)}px`;
    return box;
  }

  const when = (ts) => new Date(ts * 1000).toLocaleDateString([], {day: "numeric", month: "short"});
  const SOURCES = {promises: "promises", meetings: "meetings", email: "email", slack: "Slack", calendar: "calendar", github: "GitHub"};

  async function refresh() {
    try {
      list = (await ui().request(`/api/v1/people?q=${encodeURIComponent(query)}`)).people;
    } catch {
      list = [];
    }
    renderPage();
    renderBrief();
  }

  async function show(id) {
    try {
      open = await ui().request(`/api/v1/people/profile?id=${encodeURIComponent(id)}`);
    } catch (error) {
      ui().notify(error.message, true);
      open = null;
    }
    ui().view("people");
    renderPage();
    if (open) loadTogether(false);
  }

  async function loadTogether(fresh) {
    const box = togetherBox;
    if (!box || !open) return;
    box.replaceChildren(node("p", "Thinking about what you two are working on…", "faint"));
    try {
      const summary = await ui().request("/api/v1/people/together", "POST", {id: open.id, fresh});
      box.replaceChildren();
      if (summary.summary) box.append(node("p", summary.summary, "pp-summary"));
      else box.append(node("p", "Not much to go on yet. It fills in as you work together.", "faint"));
      if (summary.next) box.append(node("p", `Next: ${summary.next}`, "pp-next"));
    } catch (error) {
      box.replaceChildren(node("p", error.message, "faint"));
    }
  }

  async function act(path, body, message) {
    try {
      const result = await ui().request(path, "POST", body);
      if (message) ui().notify(message);
      return result;
    } catch (error) {
      ui().notify(error.message, true);
      return null;
    }
  }

  // The list ---------------------------------------------------------------------------------

  function renderList(root) {
    const card = node("section", undefined, "card pp-list");
    const search = node("input", undefined, "pp-search");
    search.placeholder = "Find someone";
    search.value = query;
    search.setAttribute("aria-label", "Find someone");
    search.addEventListener("input", () => {
      query = search.value;
      clearTimeout(noteTimer);
      noteTimer = setTimeout(async () => { await refresh(); const again = root.querySelector(".pp-search"); if (again) { again.focus(); again.setSelectionRange(query.length, query.length); } }, 250);
    });
    card.append(search);
    if (!list.length) {
      card.append(node("p", query ? "Nobody by that name yet." : "Bridge learns the people you work with from your promises, meetings, email, Slack, calendar and GitHub. Check back after a day of work.", "hint"));
    }
    for (const p of list) {
      const row = button("", "pp-row", () => show(p.id));
      const words = node("span", undefined, "pp-row-words");
      const from = Object.keys(p.seen || {}).map((k) => SOURCES[k] || k).join(", ");
      words.append(node("strong", p.name), node("span", [p.emails[0], from && `from ${from}`].filter(Boolean).join(" · "), "faint"));
      row.append(face(p.name), words, node("span", when(p.last_seen), "mono faint"));
      card.append(row);
    }
    root.append(card);
  }

  // A profile --------------------------------------------------------------------------------

  function section(title, items, render) {
    if (!items || !items.length) return null;
    const box = node("div", undefined, "pp-section");
    box.append(node("h3", title));
    const ul = node("ul");
    for (const item of items) ul.append(render(item));
    box.append(ul);
    return box;
  }

  function due(item) {
    return item.due ? ` · due ${item.due}` : "";
  }

  function renderProfile(root) {
    const p = open;
    const f = p.facts;
    const card = node("section", undefined, "card pp-profile");
    const top = node("div", undefined, "pp-top");
    top.append(button("‹ Everyone", "link small", () => { open = null; renderPage(); }));
    card.append(top);
    const head = node("div", undefined, "pp-head");
    const name = node("h2", p.name);
    name.title = "Click to rename";
    name.tabIndex = 0;
    name.addEventListener("click", async () => {
      const next = prompt("Their name", p.name);
      if (next && next.trim() && next.trim() !== p.name) {
        if (await act("/api/v1/people/rename", {id: p.id, name: next.trim()})) show(p.id);
      }
    });
    const words = node("div", undefined, "pp-head-words");
    words.append(name, node("p", [...p.emails, ...p.handles.map((h) => `@${h}`)].join(" · ") || "No address yet", "faint"));
    head.append(face(p.name, 56), words);
    card.append(head);

    togetherBox = node("div", undefined, "pp-together");
    const togetherSection = node("div", undefined, "pp-section");
    const togetherHead = node("div", undefined, "pp-section-head");
    togetherHead.append(node("h3", "Working on together"), button("Refresh", "link small", () => loadTogether(true)));
    togetherSection.append(togetherHead, togetherBox);
    card.append(togetherSection);

    for (const part of [
      section("They owe you", f.open.they_owe, (i) => node("li", `${i.what}${due(i)}`)),
      section("You owe them", f.open.you_owe, (i) => node("li", `${i.what}${due(i)}`)),
      section("Waiting for your review", f.reviews, (r) => { const li = node("li"); li.append(link(`${r.title} (${r.repo.split("/").pop()}#${r.number})`, r.url)); return li; }),
      section("Coming up together", f.upcoming, (u) => node("li", `${u.title} · ${new Date(u.start).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"})}`)),
      section("Meetings together", f.meetings, (m) => {
        const li = node("li");
        li.append(button(`${m.title} · ${when(m.when)}`, "link small", () => { ui().view("meetings"); if (window.BridgeMeetings) window.BridgeMeetings.show(m.id); }));
        if (m.their_actions.length) li.append(node("span", ` — they took on: ${m.their_actions.join("; ")}`, "faint"));
        return li;
      }),
      section("Recent messages", f.messages, (m) => {
        const li = node("li");
        li.append(node("strong", m.subject || (m.source === "slack" ? "Slack" : "Email")), node("span", ` — ${m.summary}`, "faint"));
        return li;
      }),
    ]) if (part) card.append(part);
    if (!f.open.they_owe.length && !f.open.you_owe.length && !f.meetings.length && !f.messages.length && !f.reviews.length) {
      card.append(node("p", "Nothing open between you right now.", "hint"));
    }

    const noteBox = node("div", undefined, "pp-section");
    noteBox.append(node("h3", "Your note"));
    const note = node("textarea", undefined, "pp-note");
    note.value = p.note;
    note.rows = 3;
    note.placeholder = "Anything to remember: how they like to work, what they care about, who they report to…";
    note.setAttribute("aria-label", `Your note about ${p.name}`);
    const saved = node("span", "", "faint pp-saved");
    note.addEventListener("input", () => {
      saved.textContent = "Saving…";
      clearTimeout(noteTimer);
      noteTimer = setTimeout(async () => {
        if (await act("/api/v1/people/note", {id: p.id, note: note.value})) saved.textContent = "Saved";
      }, 700);
    });
    noteBox.append(note, saved);
    card.append(noteBox);

    const tools = node("div", undefined, "pp-tools");
    const others = list.filter((x) => x.id !== p.id);
    if (others.length) {
      const pick = node("select", undefined, "small-select");
      pick.setAttribute("aria-label", "Same person as");
      pick.append(node("option", "Same person as…"));
      for (const other of others) {
        const option = node("option", `${other.name}${other.emails[0] ? ` (${other.emails[0]})` : ""}`);
        option.value = other.id;
        pick.append(option);
      }
      pick.addEventListener("change", async () => {
        if (!pick.value || pick.selectedIndex === 0) return;
        const other = others.find((x) => x.id === pick.value);
        if (!confirm(`Merge ${other.name} into ${p.name}? Their addresses and notes become one person.`)) { pick.selectedIndex = 0; return; }
        if (await act("/api/v1/people/merge", {keep: p.id, other: other.id}, "Merged.")) { await refresh(); show(p.id); }
      });
      tools.append(pick);
    }
    tools.append(button("Forget them", "ghost small", async () => {
      if (!confirm(`Forget ${p.name}? Bridge deletes what it learned and won't track them again.`)) return;
      if (await act("/api/v1/people/forget", {id: p.id}, "Forgotten.")) { open = null; refresh(); }
    }));
    card.append(tools);
    root.append(card);
  }

  function renderPage() {
    const root = $("people-root");
    if (!root) return;
    root.replaceChildren();
    if (open) renderProfile(root);
    else renderList(root);
  }

  // Today: before your next meeting ---------------------------------------------------------

  async function renderBrief() {
    const card = $("people-brief");
    if (!card) return;
    let found = null;
    try { found = (await ui().request("/api/v1/people/brief")).brief; } catch { /* no brief */ }
    card.replaceChildren();
    if (!found || !found.brief.length) {
      card.hidden = true;
      return;
    }
    const start = new Date(found.event.start);
    const head = node("div", undefined, "card-head");
    const words = node("div");
    words.append(node("p", `Before ${start.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`, "label"), node("h2", found.event.title));
    head.append(words);
    const bullets = node("ul", undefined, "pp-brief");
    for (const line of found.brief) bullets.append(node("li", line));
    const chips = node("div", undefined, "pp-chips");
    for (const person of found.people) {
      const chip = button("", "pp-chip", () => show(person.id));
      chip.append(face(person.name, 20), document.createTextNode(person.name));
      chips.append(chip);
    }
    card.append(head, bullets, chips);
    card.hidden = false;
  }

  window.BridgePeople = {refresh, show};
})();
