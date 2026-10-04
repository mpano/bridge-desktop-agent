"use strict";

// The brief at the top of Today: in the morning, your 3 for today and the day at a glance;
// in the evening, what got done, what slipped (one tap to tomorrow) and tomorrow at a glance.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let brief = null;
  let loading = false;

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

  const time = (iso) => new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
  // "today", "yesterday", "Friday" (this past week) or "2 Oct".
  function friendly(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    const day = new Date(y, m - 1, d);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((today - day) / 86400000);
    if (days === 0) return "today";
    if (days === 1) return "yesterday";
    if (days > 1 && days < 7) return day.toLocaleDateString([], {weekday: "long"});
    return day.toLocaleDateString([], {day: "numeric", month: "short"});
  }

  const hiddenKey = () => `bridge-brief-hidden-${brief.day}-${brief.kind}`;

  function hidden() {
    try { return localStorage.getItem(hiddenKey()) === "1"; } catch { return false; }
  }

  async function refresh(force = false) {
    if (loading) return;
    loading = true;
    try {
      brief = await ui().request(`/api/v1/brief${force ? "?refresh=true" : ""}`);
      render();
    } catch {
      $("brief-card").hidden = true;
    } finally {
      loading = false;
    }
  }

  async function moveToNextDay(ids) {
    try {
      const moved = await ui().request("/api/v1/brief/move", "POST", {ids});
      const day = new Date(`${moved.to}T12:00:00`).toLocaleDateString([], {weekday: "long"});
      ui().notify(`Moved ${moved.moved === 1 ? "it" : `${moved.moved} promises`} to ${day}.`);
    } catch (error) {
      ui().notify(error.message, true);
    }
    await refresh();
    if (window.BridgePromises) window.BridgePromises.refresh();
  }

  async function markDone(id) {
    try {
      await ui().request("/api/v1/commitments/update", "POST", {id, status: "done"});
      ui().notify("Done. Nice.");
    } catch (error) {
      ui().notify(error.message, true);
    }
    await refresh();
    if (window.BridgePromises) window.BridgePromises.refresh();
  }

  function fact(text, onClick) {
    return onClick ? button(text, "brief-fact link", onClick) : node("span", text, "brief-fact");
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  // Morning -------------------------------------------------------------------------------------

  function renderMorning() {
    $("brief-label").textContent = "Morning brief";
    $("brief-title").textContent = brief.headline || (brief.top.length ? "Your 3 for today" : "A clear day");
    const body = $("brief-body");
    body.replaceChildren();
    if (brief.top.length) {
      const list = node("ol", undefined, "brief-top");
      brief.top.forEach((item, index) => {
        const li = node("li", undefined, `brief-item ${item.kind}`);
        li.append(node("span", String(index + 1), "brief-n"));
        const words = node("div", undefined, "brief-words");
        words.append(node("strong", item.title), node("span", item.why, "muted"));
        li.append(words);
        if (item.kind === "promise") li.append(button("Done", "secondary small", () => markDone(item.ref.id)));
        else if (item.kind === "email" || item.kind === "slack") li.append(button("Open", "secondary small", () => ui().view("inbox")));
        else if ((item.kind === "review" || item.kind === "ticket") && item.ref.url) {
          const link = node("a", "Open", "secondary small button-link");
          link.href = item.ref.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          li.append(link);
        } else if (item.kind === "waiting" && window.BridgePromises) {
          li.append(button("Follow up", "secondary small", () => window.BridgePromises.show("theirs")));
        }
        list.append(li);
      });
      body.append(list);
    }
    const facts = node("div", undefined, "brief-facts");
    const meetings = brief.meetings;
    if (meetings.count !== null) {
      facts.append(fact(meetings.count
        ? `${plural(meetings.count, "meeting")}${meetings.first ? ` · next ${meetings.first.title} at ${time(meetings.first.start)}` : ""}`
        : "No meetings today"));
    }
    if (brief.due.length) facts.append(fact(`${plural(brief.due.length, "promise")} due`));
    const replies = brief.inbox.urgent + brief.inbox.reply;
    if (replies) facts.append(fact(`${replies} need${replies === 1 ? "s" : ""} a reply`, () => ui().view("inbox")));
    if (brief.waiting.length) facts.append(fact(`Waiting on ${brief.waiting.map((c) => c.person).slice(0, 2).join(", ")}`));
    if (facts.children.length) body.append(facts);
  }

  // Evening -------------------------------------------------------------------------------------

  function renderEvening() {
    $("brief-label").textContent = "Evening wrap-up";
    $("brief-title").textContent = brief.headline;
    const body = $("brief-body");
    body.replaceChildren();
    const next = brief.tomorrow.name;
    if (brief.done.length) {
      const names = brief.done.slice(0, 3).map((c) => c.what).join(" · ");
      body.append(node("p", `✓ ${names}${brief.done.length > 3 ? ` +${brief.done.length - 3}` : ""}`, "brief-done"));
    }
    if (brief.slipped.length) {
      const list = node("ul", undefined, "brief-slipped");
      for (const item of brief.slipped) {
        const li = node("li", undefined, "brief-item");
        const words = node("div", undefined, "brief-words");
        words.append(node("strong", item.what), node("span", `to ${item.person} · was due ${friendly(item.due)}`, "muted"));
        li.append(words, button(`Move to ${next}`, "secondary small", () => moveToNextDay([item.id])),
          button("Done", "ghost small", () => markDone(item.id)));
        list.append(li);
      }
      body.append(node("p", "Slipped", "label attention"), list);
      if (brief.slipped.length > 1) {
        body.append(button(`Move all ${brief.slipped.length} to ${next}`, "primary small brief-all",
          () => moveToNextDay(brief.slipped.map((c) => c.id))));
      }
    }
    const facts = node("div", undefined, "brief-facts");
    const m = brief.tomorrow.meetings;
    const day = next.charAt(0).toUpperCase() + next.slice(1);
    if (m.count !== null) {
      facts.append(fact(m.count ? `${day}: ${plural(m.count, "meeting")}${m.first ? `, first ${m.first.title} at ${time(m.first.start)}` : ""}` : `${day}: no meetings`));
    }
    if (brief.tomorrow.due.length) facts.append(fact(`${plural(brief.tomorrow.due.length, "promise")} due ${next}`));
    const replies = brief.inbox.urgent + brief.inbox.reply;
    if (replies) facts.append(fact(`${replies} still need${replies === 1 ? "s" : ""} a reply`, () => ui().view("inbox")));
    if (brief.waiting.length) facts.append(fact(`Still waiting on ${brief.waiting.map((c) => c.person).slice(0, 2).join(", ")}`));
    if (facts.children.length) body.append(facts);
  }

  function render() {
    $("brief-card").hidden = !brief || hidden();
    if ($("brief-card").hidden) return;
    $("brief-card").classList.toggle("evening", brief.kind === "evening");
    $("brief-refresh").hidden = brief.kind !== "morning";
    if (brief.kind === "evening") renderEvening(); else renderMorning();
  }

  $("brief-hide").addEventListener("click", () => {
    try { localStorage.setItem(hiddenKey(), "1"); } catch { /* private mode */ }
    $("brief-card").hidden = true;
  });
  $("brief-refresh").addEventListener("click", () => refresh(true));

  window.BridgeBrief = {refresh: () => refresh(false)};
})();
