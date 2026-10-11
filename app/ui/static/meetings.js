"use strict";

// Meeting notes: the Meetings page (what's recording now, past meetings with their summary,
// decisions, action items, follow-up email and transcript) and a small card on Today (take
// notes for the meeting happening now, recording, notes ready).
(() => {
  const {button, node} = window.BridgeDOM;
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;
  let data = null;
  let open = null;      // The meeting shown in full.
  let timer = null;
  let filter = "";

  const clock = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  const when = (ts) => new Date(ts * 1000).toLocaleString([], {weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});

  async function act(path, body, message) {
    try {
      const result = await ui().request(path, "POST", body || {});
      if (message) ui().notify(message);
      await refresh();
      return result;
    } catch (error) {
      ui().notify(error.message, true);
      return null;
    }
  }

  async function refresh() {
    try {
      data = await ui().request("/api/v1/meetings");
    } catch {
      data = null;
    }
    render();
    clearTimeout(timer);
    const busy = data && (data.current || data.meetings.some((m) => m.status === "processing"));
    if (busy) timer = setTimeout(refresh, 2000);
  }

  function lines(list) {
    const box = node("div", undefined, "mt-lines");
    for (const line of list) {
      const row = node("p", undefined, `mt-line ${line.who === "You" ? "you" : "them"}`);
      row.append(node("span", line.who, "mt-who"), document.createTextNode(line.text));
      box.append(row);
    }
    return box;
  }

  function askBox(id) {
    const form = node("form", undefined, "mt-ask");
    const input = node("input");
    input.placeholder = id ? "Ask about this meeting…" : "Ask about the meeting so far…";
    input.maxLength = 500;
    input.setAttribute("aria-label", input.placeholder);
    const answer = node("p", undefined, "mt-answer");
    answer.hidden = true;
    form.append(input, button("Ask", "secondary small", () => form.requestSubmit()));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const question = input.value.trim();
      if (!question) return;
      answer.hidden = false;
      answer.textContent = "Thinking…";
      try {
        const reply = await ui().request("/api/v1/meetings/ask", "POST", id ? {question, id} : {question});
        answer.textContent = reply.answer;
      } catch (error) {
        answer.textContent = error.message;
      }
    });
    const wrap = node("div");
    wrap.append(form, answer);
    return wrap;
  }

  // The meeting being recorded ---------------------------------------------------------------

  function live(current) {
    const card = node("section", undefined, "card mt-live");
    const head = node("div", undefined, "mt-live-head");
    head.append(node("span", undefined, `mt-rec${current.paused ? " paused" : ""}`),
      node("strong", current.title, "grow"),
      node("span", current.paused ? "Paused" : clock(current.minutes * 60), "mono faint"));
    card.append(head);
    if (current.problem) card.append(node("p", current.problem, "mt-warn"));
    card.append(node("p", "Let everyone know you're taking notes.", "hint"));
    if (current.notes.length) {
      const notes = node("ul", undefined, "mt-notes");
      for (const note of current.notes) notes.append(node("li", note));
      card.append(node("p", "Live notes", "label"), notes);
    }
    card.append(current.lines.length ? lines(current.lines) : node("p", "Listening… the first words appear in a few seconds.", "faint mt-wait"));
    card.append(askBox(null));
    const row = node("div", undefined, "row-actions");
    row.append(
      button(current.paused ? "Resume" : "Pause", "secondary small", () => act("/api/v1/meetings/pause", {paused: !current.paused})),
      button("Stop & write it up", "primary small", () => act("/api/v1/meetings/stop", {}, "Writing the summary…")),
    );
    card.append(row);
    return card;
  }

  function startCard() {
    const card = node("section", undefined, "card mt-start");
    if (!data.enabled) {
      card.append(node("h2", "Take notes in your meetings"),
        node("p", "Bridge listens to your microphone and the call's sound, writes it down on this Mac, and when it ends gives you a summary, decisions, action items in Promises and a follow-up email. Audio is never saved; the words go to OpenAI for the summary.", "hint"),
        button("Turn on Meeting notes", "primary small", async () => {
          await ui().request("/api/v1/settings/app", "POST", {meetings_enabled: true}).catch((error) => ui().notify(error.message, true));
          refresh();
        }));
      return card;
    }
    if (!data.available) {
      card.append(node("h2", "Meeting notes"), node("p", "Taking notes works in the Bridge app on your Mac.", "hint"));
      return card;
    }
    const suggestion = data.suggestion;
    card.append(node("h2", suggestion ? `Meeting now: ${suggestion.title}` : "Take notes"));
    card.append(node("p", suggestion ? `${suggestion.attendees.length ? `With ${suggestion.attendees.slice(0, 3).map((p) => p.name).join(", ")}. ` : ""}Bridge writes it down and wraps it up when it ends.`
      : "Start when your meeting starts. Bridge names it after your calendar.", "hint"));
    const row = node("div", undefined, "row-actions");
    row.append(button("● Take notes", "primary small", () => act("/api/v1/meetings/start", {}, "Taking notes. Let everyone know.")));
    card.append(row);
    return card;
  }

  // A past meeting --------------------------------------------------------------------------

  function section(title, items, render) {
    if (!items || !items.length) return null;
    const box = node("div", undefined, "mt-section");
    box.append(node("h3", title));
    const list = node("ul");
    for (const item of items) list.append(render(item));
    box.append(list);
    return box;
  }

  function followUp(meeting) {
    const draft = (meeting.summary && meeting.summary.followup) || {subject: "", body: ""};
    if (!draft.body) return null;
    const box = node("div", undefined, "mt-section mt-follow");
    box.append(node("h3", "Follow-up email"));
    const to = node("input");
    to.value = meeting.attendees.map((p) => p.email).filter(Boolean).join(", ");
    to.placeholder = "name@company.com, …";
    to.setAttribute("aria-label", "To");
    const subject = node("input");
    subject.value = draft.subject;
    subject.setAttribute("aria-label", "Subject");
    const body = node("textarea");
    body.value = draft.body;
    body.rows = 8;
    body.setAttribute("aria-label", "Email");
    const row = node("div", undefined, "row-actions");
    const send = button("Send…", "primary small", () => {
      const addresses = to.value.split(/[,;\s]+/).filter(Boolean);
      if (!addresses.length) return to.focus();
      row.replaceChildren(node("span", `Send to ${addresses.join(", ")}?`, "mt-confirm"),
        button("Send now", "primary small", async () => {
          const sent = await act("/api/v1/meetings/followup", {id: meeting.id, to: addresses, subject: subject.value, body: body.value}, "Sent.");
          if (sent) show(meeting.id);
        }),
        button("Cancel", "ghost small", () => show(meeting.id)));
    });
    row.append(button("Copy", "secondary small", async () => {
      try { await navigator.clipboard.writeText(`${subject.value}\n\n${body.value}`); ui().notify("Copied."); } catch { /* not allowed */ }
    }), send);
    box.append(node("label", "To", "mt-field"), to, node("label", "Subject", "mt-field"), subject, body, row);
    if (meeting.sent.length) box.append(node("p", `✓ Sent to ${meeting.sent[meeting.sent.length - 1].to.join(", ")}`, "mt-sent"));
    return box;
  }

  async function show(id) {
    try {
      open = await ui().request(`/api/v1/meetings/detail?id=${encodeURIComponent(id)}`);
    } catch (error) {
      ui().notify(error.message, true);
      open = null;
    }
    render();
  }

  function detail(meeting) {
    const card = node("section", undefined, "card mt-detail");
    const head = node("div", undefined, "mt-detail-head");
    head.append(button("‹ All meetings", "link small", () => { open = null; render(); }), node("span", "", "grow"),
      button("Delete", "ghost small", async () => {
        if (!confirm(`Delete the notes of “${meeting.title}”?`)) return;
        open = null;
        await act("/api/v1/meetings/delete", {id: meeting.id}, "Deleted.");
      }));
    card.append(head, node("h2", meeting.title), node("p", `${when(meeting.started)} · ${Math.round(meeting.minutes)} min${meeting.attendees.length ? ` · with ${meeting.attendees.map((p) => p.name).slice(0, 4).join(", ")}` : ""}`, "faint"));
    const s = meeting.summary || {};
    if (meeting.status === "processing") card.append(node("p", "Writing the summary…", "mt-wait"));
    if (s.failed) card.append(node("p", "The summary couldn't be written. The transcript is below.", "mt-warn"));
    if (s.summary) card.append(node("p", s.summary, "mt-summary"));
    for (const part of [
      section("Decisions", s.decisions, (d) => node("li", d)),
      section("Action items", s.actions, (a) => {
        const li = node("li", undefined, "mt-action");
        li.append(node("strong", a.owner === "you" ? "You" : a.person), document.createTextNode(` ${a.what}`));
        if (a.due) li.append(node("span", ` · by ${a.due}`, "faint"));
        return li;
      }),
      section("Open questions", s.questions, (q) => node("li", q)),
    ]) if (part) card.append(part);
    if (meeting.promises) card.append(node("p", `✓ ${meeting.promises} added to Promises on Today.`, "mt-sent"));
    const follow = followUp(meeting);
    if (follow) card.append(follow);
    card.append(askBox(meeting.id));
    const transcript = node("details", undefined, "mt-transcript");
    transcript.append(node("summary", `Transcript · ${meeting.lines.length} lines`));
    const search = node("input");
    search.placeholder = "Find in the transcript";
    search.value = filter;
    search.setAttribute("aria-label", "Find in the transcript");
    const box = node("div");
    const paint = () => {
      const words = search.value.trim().toLowerCase();
      filter = search.value;
      box.replaceChildren(lines(meeting.lines.filter((l) => !words || l.text.toLowerCase().includes(words))));
    };
    search.addEventListener("input", paint);
    transcript.append(search, box);
    paint();
    card.append(transcript);
    return card;
  }

  function list() {
    const card = node("section", undefined, "card mt-list");
    card.append(node("h2", "Past meetings"));
    if (!data.meetings.length) {
      card.append(node("p", "No notes yet. They're kept for " + data.keep_days + " days.", "hint"));
      return card;
    }
    for (const m of data.meetings) {
      const row = button("", "mt-row", () => show(m.id));
      const words = node("span", undefined, "mt-row-words");
      words.append(node("strong", m.title), node("span", `${when(m.started)} · ${Math.round(m.minutes)} min${m.actions ? ` · ${m.actions} action item${m.actions > 1 ? "s" : ""}` : ""}`, "faint"));
      if (m.status === "processing") words.append(node("span", "Writing the summary…", "mt-wait"));
      else if (m.summary) words.append(node("span", m.summary, "mt-row-summary"));
      row.append(words, node("span", "›", "faint"));
      card.append(row);
    }
    card.append(node("p", `Notes are kept for ${data.keep_days} days. Audio is never saved.`, "hint mt-keep"));
    return card;
  }

  function renderPage() {
    const root = $("meetings-root");
    if (!root) return;
    root.replaceChildren();
    if (!data) return;
    if (open) {
      root.append(detail(open));
      return;
    }
    root.append(data.current ? live(data.current) : startCard(), list());
  }

  // Today: take notes now, recording, notes ready ---------------------------------------------

  function renderToday() {
    const card = $("meeting-card");
    if (!card) return;
    card.replaceChildren();
    let shown = false;
    if (data && data.current) {
      card.append(node("span", undefined, "mt-rec"), node("strong", `Taking notes: ${data.current.title}`, "grow"),
        node("span", clock(data.current.minutes * 60), "mono faint"),
        button("Open", "secondary small", () => ui().view("meetings")));
      shown = true;
    } else if (data && data.enabled && data.available && data.suggestion) {
      card.append(node("strong", `Meeting now: ${data.suggestion.title}`, "grow"),
        button("● Take notes", "primary small", () => act("/api/v1/meetings/start", {}, "Taking notes. Let everyone know.")));
      shown = true;
    } else if (data) {
      const recent = data.meetings.find((m) => m.status === "done" && Date.now() / 1000 - m.started < 6 * 3600);
      if (recent) {
        card.append(node("strong", `Notes ready: ${recent.title}`, "grow"),
          node("span", recent.actions ? `${recent.actions} action item${recent.actions > 1 ? "s" : ""}` : "", "faint"),
          button("Read", "secondary small", () => { ui().view("meetings"); show(recent.id); }));
        shown = true;
      }
    }
    card.hidden = !shown;
  }

  function render() {
    renderPage();
    renderToday();
  }

  window.BridgeMeetings = {refresh, show};
})();
