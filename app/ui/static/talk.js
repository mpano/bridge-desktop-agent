"use strict";

// Talking with Bridge: a live voice conversation over WebRTC with OpenAI Realtime. Used by the
// Mac's talk panel and by Ask on the phone. Audio streams straight between this page and OpenAI
// (the browser cancels Bridge's own voice, so you can interrupt it); Bridge's tools run on the
// Mac through /api/v1/talk/tool, with the same approvals and privacy rules as Ask.
window.BridgeTalk = (() => {
  const {button, node} = window.BridgeDOM;
  const IDLE_SECONDS = 75;  // Quiet this long and the conversation ends by itself.
  let ui = null;            // {request, root, onEnd}
  let session = null;       // {id, pc, channel, stream, audio, started, ...}
  let lines = [];
  let state = "idle";

  const WORDS = {
    connecting: "Connecting…",
    listening: "Listening",
    hearing: "Listening…",
    thinking: "Thinking…",
    working: "Working on it…",
    speaking: "Speaking",
    muted: "Muted",
    ended: "Ended",
    failed: "Couldn't connect",
  };

  // Drawing ------------------------------------------------------------------------------------

  function render() {
    if (!ui) return;
    const root = ui.root;
    root.querySelector(".talk-state").textContent = WORDS[state] || "";
    root.dataset.state = state;
    const list = root.querySelector(".talk-lines");
    list.replaceChildren(...lines.slice(-4).map((line) => {
      const item = node("p", line.text, `talk-line ${line.role}`);
      return item;
    }));
    const mute = root.querySelector(".talk-mute");
    if (mute) mute.textContent = session && session.muted ? "Unmute" : "Mute";
  }

  function setState(next) {
    state = next;
    render();
  }

  function say(role, text) {
    if (!text) return;
    lines.push({role, text});
    lines = lines.slice(-12);
    render();
    if (session && (role === "user" || role === "assistant")) {
      ui.request("/api/v1/talk/said", "POST", {session_id: session.id, role, text}).catch(() => {});
    }
  }

  function showCard(text, approval) {
    const card = ui.root.querySelector(".talk-card");
    card.replaceChildren();
    card.hidden = !text && !approval;
    if (text) card.append(node("p", text, "talk-card-text"));
    if (approval) {
      const row = node("div", undefined, "row-actions");
      row.append(
        button("Go ahead", "primary small", () => decide(true)),
        button("Don't", "ghost small", () => decide(false)),
      );
      card.append(row);
    }
  }

  // The conversation -------------------------------------------------------------------------

  function send(event) {
    if (session && session.channel && session.channel.readyState === "open") {
      session.channel.send(JSON.stringify(event));
    }
  }

  function touch() {
    if (session) session.lastActivity = Date.now();
  }

  async function start(options) {
    if (session) return;
    ui = options;
    lines = [];
    showCard("");
    setState("connecting");
    let made;
    try {
      made = await ui.request("/api/v1/talk/start", "POST", {name: options.name || ""});
    } catch (error) {
      setState("failed");
      showCard(error.message);
      return;
    }
    const pc = new RTCPeerConnection();
    const audio = document.createElement("audio");
    audio.autoplay = true;
    pc.ontrack = (event) => { audio.srcObject = event.streams[0]; };
    session = {id: made.session_id, pc, audio, started: Date.now(), lastActivity: Date.now(), calls: 0, muted: false, ending: false,
      tokens: {input: 0, cached: 0, output: 0}};
    try {
      session.stream = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true},
      });
    } catch {
      await stop("Bridge needs the microphone. Allow it, then try again.");
      return;
    }
    session.stream.getTracks().forEach((track) => pc.addTrack(track, session.stream));
    const channel = pc.createDataChannel("oai-events");
    session.channel = channel;
    channel.addEventListener("message", (event) => {
      try { handle(JSON.parse(event.data)); } catch { /* ignore a bad event */ }
    });
    channel.addEventListener("open", () => setState("listening"));
    pc.addEventListener("connectionstatechange", () => {
      if (session && ["failed", "disconnected"].includes(pc.connectionState)) stop("The connection dropped.");
    });
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const answer = await fetch(made.calls_url, {
        method: "POST",
        body: offer.sdp,
        headers: {Authorization: `Bearer ${made.client_secret}`, "Content-Type": "application/sdp"},
      });
      if (!answer.ok) throw new Error(`OpenAI said ${answer.status}`);
      await pc.setRemoteDescription({type: "answer", sdp: await answer.text()});
    } catch {
      await stop("Couldn't connect to OpenAI. Try again in a moment.");
      return;
    }
    session.idleTimer = setInterval(() => {
      if (!session || state === "speaking" || state === "working") return;
      if (Date.now() - session.lastActivity > IDLE_SECONDS * 1000) stop("Ended after a quiet minute. Say “Hey Bridge” to talk again.");
    }, 5000);
  }

  function handle(event) {
    switch (event.type) {
      case "input_audio_buffer.speech_started":
        touch();
        showCard("");
        setState("hearing");
        break;
      case "input_audio_buffer.speech_stopped":
        touch();
        setState("thinking");
        break;
      case "conversation.item.input_audio_transcription.completed":
        say("user", (event.transcript || "").trim());
        break;
      case "output_audio_buffer.started":
        setState("speaking");
        break;
      case "output_audio_buffer.stopped":
        touch();
        if (session && session.ending) stop("");
        else setState(session && session.muted ? "muted" : "listening");
        break;
      case "response.output_audio_transcript.done":
        say("assistant", (event.transcript || "").trim());
        break;
      case "response.done":
        countTokens(event.response && event.response.usage);
        runCalls(event.response);
        break;
      case "error":
        showCard((event.error && event.error.message) || "Something went wrong.");
        break;
      default:
        break;
    }
  }

  // OpenAI reports what each answer used; Bridge adds it up for Settings › Usage.
  function countTokens(usage) {
    if (!session || !usage) return;
    const cached = ((usage.input_token_details || {}).cached_tokens) || 0;
    session.tokens.input += usage.input_tokens || 0;
    session.tokens.cached += cached;
    session.tokens.output += usage.output_tokens || 0;
  }

  async function runCalls(response) {
    const calls = ((response && response.output) || []).filter((item) => item.type === "function_call");
    if (!calls.length) return;
    let answered = 0;
    for (const call of calls) {
      if (call.name === "end_conversation") {
        session.ending = true;
        send({type: "conversation.item.create", item: {type: "function_call_output", call_id: call.call_id, output: "{\"ok\":true}"}});
        // Let the goodbye finish playing; end anyway if nothing is playing.
        setTimeout(() => { if (session && session.ending && state !== "speaking") stop(""); }, 1500);
        continue;
      }
      setState("working");
      let result;
      try {
        let args = {};
        try { args = JSON.parse(call.arguments || "{}"); } catch { args = {}; }
        result = await ui.request("/api/v1/talk/tool", "POST", {session_id: session.id, name: call.name, arguments: args});
      } catch (error) {
        result = {for_model: {success: false, error: error.message}, display: ""};
      }
      if (!session) return;
      showCard(result.display || "", result.approval);
      send({type: "conversation.item.create", item: {type: "function_call_output", call_id: call.call_id, output: JSON.stringify(result.for_model)}});
      answered += 1;
    }
    if (answered && session && !session.ending) send({type: "response.create"});
    touch();
  }

  async function decide(approved) {
    if (!session) return;
    showCard(approved ? "Going ahead…" : "");
    let result;
    try {
      result = await ui.request("/api/v1/talk/approve", "POST", {session_id: session.id, approve: approved});
    } catch (error) {
      result = {for_model: {success: false, error: error.message}, display: error.message};
    }
    showCard(result.display || "");
    // Tell the model what happened on screen, so it doesn't ask again.
    send({type: "conversation.item.create", item: {type: "message", role: "system", content: [{type: "input_text",
      text: `The user tapped ${approved ? "Go ahead" : "Don't"} on the screen. Result: ${JSON.stringify(result.for_model)}`}]}});
    send({type: "response.create"});
    touch();
  }

  function mute() {
    if (!session || !session.stream) return;
    session.muted = !session.muted;
    session.stream.getAudioTracks().forEach((track) => { track.enabled = !session.muted; });
    setState(session.muted ? "muted" : "listening");
  }

  async function stop(message) {
    const ending = session;
    session = null;
    if (ending) {
      clearInterval(ending.idleTimer);
      try { ending.channel && ending.channel.close(); } catch { /* closing */ }
      try { ending.pc.close(); } catch { /* closing */ }
      if (ending.stream) ending.stream.getTracks().forEach((track) => track.stop());
      ending.audio.srcObject = null;
      const seconds = (Date.now() - ending.started) / 1000;
      ui.request("/api/v1/talk/end", "POST", {session_id: ending.id, seconds, tokens: ending.tokens}).catch(() => {});
    }
    setState(message ? "ended" : "ended");
    if (message) showCard(message);
    if (ui && ui.onEnd) ui.onEnd(message);
  }

  // The markup both pages use.
  function mount(root) {
    root.classList.add("talk");
    root.replaceChildren();
    const orb = node("div", undefined, "talk-orb");
    orb.setAttribute("aria-hidden", "true");
    orb.append(node("span"), node("span"), node("span"));
    const head = node("div", undefined, "talk-head");
    head.append(orb, node("span", "", "talk-state"));
    const lines = node("div", undefined, "talk-lines");
    lines.setAttribute("aria-live", "polite");
    const card = node("div", undefined, "talk-card");
    card.hidden = true;
    const controls = node("div", undefined, "talk-controls");
    controls.append(button("Mute", "secondary small talk-mute", mute), button("End", "danger-soft small talk-end", () => stop("")));
    root.append(head, lines, card, controls);
  }

  return {start, stop: () => stop(""), mount, active: () => Boolean(session), state: () => state};
})();
