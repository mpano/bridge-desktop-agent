"use strict";

// Bridge on your desk: the avatar in the corner of the screen. It shows a countdown before
// your next meeting, a badge when something needs you, pops up a bubble when a notification
// arrives, and dozes after hours. Click to talk, hover for what's next, right-click for more,
// drag to move. The window is only as big as what's showing (the app resizes it to fit).
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  const avatar = window.BridgeAvatar.create($("bd-avatar"), undefined, {size: 64});
  let state = null;
  let seen = null;  // Notification ids already shown (null until the first look).
  let reacting = null;
  let hoverTimer = null;
  let bubbleTimer = null;

  // The window fits what's visible, measured from its bottom-right corner.
  function fit() {
    requestAnimationFrame(() => {
      let width = 0;
      let height = 0;
      for (const element of document.querySelectorAll("#bd > *:not([hidden]), .bd-row > *:not([hidden])")) {
        const rect = element.getBoundingClientRect();
        if (!rect.width) continue;
        width = Math.max(width, window.innerWidth - rect.left);
        height = Math.max(height, window.innerHeight - rect.top);
      }
      mini.native({action: "buddy_size", width: Math.ceil(width + 8), height: Math.ceil(height + 8)});
    });
  }

  function minutesText(minutes) {
    if (minutes <= 0) return "now";
    if (minutes < 60) return `${minutes}m`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60 ? `${minutes % 60}m` : ""}`.trim();
  }

  function mood() {
    if (reacting) return "speaking";
    if (!state) return "idle";
    if (state.sleeping && !state.count && !(state.meeting && state.meeting.minutes <= 15)) return "sleeping";
    if (state.meeting && state.meeting.minutes <= 5) return "thinking";
    return "idle";
  }

  function render() {
    if (!state) return;
    avatar.setColors(state.voice.colors);
    avatar.setState(mood());
    $("bd-face").setAttribute("aria-label", state.talk ? `Talk to ${state.voice.name}` : "Ask Bridge");
    $("bd-face").classList.toggle("alert", state.count > 0);
    $("bd-badge").hidden = !state.count;
    $("bd-badge").textContent = state.count > 9 ? "9+" : String(state.count);
    const chip = $("bd-chip");
    const meeting = state.meeting;
    const soon = meeting && meeting.minutes <= 15;
    chip.hidden = !soon;
    if (soon) {
      chip.textContent = meeting.now ? `Now · ${meeting.title}` : `${meeting.title} · ${minutesText(meeting.minutes)}`;
      chip.classList.toggle("urgent", meeting.minutes <= 5);
    }
    fit();
  }

  function react(note) {
    clearTimeout(bubbleTimer);
    const bubble = $("bd-bubble");
    bubble.replaceChildren(node("strong", note.title), node("span", note.body));
    bubble.onclick = () => mini.native({action: "open", view: note.view || "today"});
    bubble.hidden = false;
    reacting = setTimeout(() => { reacting = null; render(); }, 2600);
    render();
    bubbleTimer = setTimeout(() => { bubble.hidden = true; fit(); }, 8000);
  }

  async function poll() {
    try {
      state = await mini.request("/api/v1/buddy");
      const fresh = state.latest.filter((note) => seen && !seen.has(note.id));
      seen = new Set(state.latest.map((note) => note.id));
      render();
      if (fresh.length) react(fresh[fresh.length - 1]);
    } catch { /* Bridge restarting: try again */ }
    setTimeout(poll, 4000);
  }

  // Hover: what's next and what needs you.
  function showCard() {
    if (!state || !$("bd-menu").hidden) return;
    const card = $("bd-card");
    card.replaceChildren();
    const meeting = state.meeting;
    card.append(node("p", meeting ? (meeting.now ? `Now: ${meeting.title}` : `Next: ${meeting.title} in ${minutesText(meeting.minutes)}`) : "No more meetings today", "bd-next"));
    for (const item of state.needs) {
      card.append(button(item.label, "bd-need", () => mini.native({action: "open", view: item.view})));
    }
    if (!state.needs.length) card.append(node("p", state.sleeping ? "All quiet. See you tomorrow." : "Nothing needs you right now.", "faint bd-quiet"));
    const row = node("div", undefined, "bd-card-actions");
    row.append(
      button(state.talk ? "Talk" : "Ask", "primary small", () => mini.native({action: state.talk ? "buddy_talk" : "buddy_ask"})),
      button("Open Bridge", "ghost small", () => mini.native({action: "open", view: "today"})),
    );
    card.append(row);
    card.hidden = false;
    fit();
  }

  function hideCard() {
    $("bd-card").hidden = true;
    fit();
  }

  function menu() {
    clearTimeout(hoverTimer);
    hideCard();
    const box = $("bd-menu");
    box.replaceChildren();
    const item = (label, action) => {
      const entry = button(label, "bd-menu-item", () => { box.hidden = true; fit(); action(); });
      entry.setAttribute("role", "menuitem");
      box.append(entry);
    };
    if (state && state.talk) item("Talk to Bridge", () => mini.native({action: "buddy_talk"}));
    item("Ask Bridge…", () => mini.native({action: "buddy_ask"}));
    item("Open Bridge", () => mini.native({action: "open", view: "today"}));
    item("Hide from the desk", async () => {
      try { await mini.request("/api/v1/settings/app", "POST", {desk_buddy: false}); } catch { /* try again later */ }
      mini.native({action: "hide"});
    });
    box.hidden = false;
    fit();
  }

  // Click to talk; drag to move (the spot is remembered).
  const face = $("bd-face");
  let drag = null;
  face.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = {x: event.screenX, y: event.screenY, moved: false};
    face.setPointerCapture(event.pointerId);
  });
  face.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.screenX - drag.x;
    const dy = event.screenY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    drag.moved = true;
    drag.x = event.screenX;
    drag.y = event.screenY;
    mini.native({action: "buddy_drag", dx, dy});
  });
  face.addEventListener("pointerup", () => {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    if (moved) mini.native({action: "buddy_dropped"});
    else mini.native({action: state && state.talk ? "buddy_talk" : "buddy_ask"});
  });
  face.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    menu();
  });
  $("bd").addEventListener("mouseenter", () => {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(showCard, 350);
  });
  $("bd").addEventListener("mouseleave", () => {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => { hideCard(); $("bd-menu").hidden = true; fit(); }, 450);
  });

  mini.ready.then(poll);
})();
