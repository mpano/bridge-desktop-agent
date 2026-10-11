"use strict";

// "New in Bridge": once, at the top of Today, for people who finished Setup before these
// features existed. Each tile says what it is in one line and has one way to try it.
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let data = null;

  function tiles(x) {
    return [
      {icon: "🎙", title: "Meeting notes", text: "Bridge listens to your meeting, then writes the summary, decisions, action items and a follow-up email.",
        action: ["Set it up", () => ui().view("meetings")]},
      {icon: "🧭", title: "Show me how", text: "Say “show me how to …” and Bridge highlights each step on screen for you to do.",
        action: x.operator ? ["Try it", () => ui().ask("Show me how to turn on Night Shift")] : ["Turn on", () => ui().view("settings", "operator")]},
      {icon: "❓", title: "What's this? ⌃⌥W", text: "Point at any button, icon or error and press ⌃⌥W to hear what it is.",
        action: x.screen ? null : ["Allow it", () => ui().view("settings", "privacy")], note: x.screen ? "Try it on any toolbar icon." : ""},
      {icon: "🙂", title: "Bridge on your desk", text: "Your Bridge in a corner of the screen: next meeting, what needs you, click to talk.",
        action: x.desk ? null : ["Show it", async () => { await ui().request("/api/v1/settings/app", "POST", {desk_buddy: true}); load(); }],
        note: x.desk ? "Bottom-right corner — drag it anywhere." : ""},
      {icon: "⚡", title: "Recipes", text: "When something happens, Bridge does something: “when my PR merges, post it in #releases”.",
        action: ["Make one", () => ui().view("automations")]},
      {icon: "🔔", title: "Tell me when…", text: "PRs merged, tickets moved, releases, replies — on your Mac and phone.",
        action: ["Add one", () => ui().view("automations")]},
      {icon: "📝", title: "Weekly report", text: "Every Friday, a draft of your status update from what you actually did.",
        action: ["See this week's", () => window.BridgeReport && window.BridgeReport.open()]},
      {icon: "🗣", title: "Voices with faces", text: "Hear each voice, pick yours, and talk with an avatar that moves as it speaks.",
        action: ["Pick a voice", () => ui().view("settings", "voice")]},
    ];
  }

  async function seen() {
    $("whats-new").hidden = true;
    try { await ui().request("/api/v1/whats-new/seen", "POST"); } catch { /* shows again next time */ }
  }

  function render() {
    const card = $("whats-new");
    card.hidden = !(data && data.show);
    if (card.hidden) return;
    const grid = $("whats-new-tiles");
    grid.replaceChildren();
    for (const tile of tiles(data.extras)) {
      const box = node("div", undefined, "wn-tile");
      const head = node("div", undefined, "wn-head");
      head.append(node("span", tile.icon, "wn-icon"), node("strong", tile.title));
      box.append(head, node("p", tile.text, "wn-text"));
      if (tile.note) box.append(node("p", tile.note, "wn-note"));
      if (tile.action) box.append(button(tile.action[0], "secondary small", tile.action[1]));
      grid.append(box);
    }
  }

  async function load() {
    try {
      data = await ui().request("/api/v1/whats-new");
    } catch {
      data = null;
    }
    render();
  }

  $("whats-new-close").addEventListener("click", seen);
  window.BridgeWhatsNew = {refresh: load};
})();
