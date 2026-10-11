"use strict";

// Bridge's face in a conversation: a soft orb in the voice's colours with eyes and a mouth.
// It breathes while idle, blinks now and then, follows your voice with a ring while it listens,
// looks up while it thinks, and moves its mouth with the real sound of its voice. Drawn as SVG;
// levels come from Web Audio analysers (0 to 1), so it reacts to the actual audio.
window.BridgeAvatar = (() => {
  const NS = "http://www.w3.org/2000/svg";
  let counter = 0;

  function el(tag, attrs = {}) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }

  // How loud a stream or element is right now, 0–1 (RMS of the waveform).
  function meter(context, source) {
    const analyser = context.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    return () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const value of data) {
        const centred = (value - 128) / 128;
        sum += centred * centred;
      }
      return Math.min(1, Math.sqrt(sum / data.length) * 4);
    };
  }

  function create(container, colors = ["#6f7dff", "#b48cff"], {size = 120} = {}) {
    counter += 1;
    const id = `bridge-avatar-${counter}`;
    const svg = el("svg", {viewBox: "0 0 120 120", width: size, height: size, class: "avatar-svg", "aria-hidden": "true"});
    const defs = el("defs");
    const gradient = el("radialGradient", {id, cx: "35%", cy: "30%", r: "75%"});
    const light = el("stop", {offset: "0"});
    const deep = el("stop", {offset: "1"});
    gradient.append(light, deep);
    defs.append(gradient);
    const ring = el("circle", {cx: 60, cy: 60, r: 54, fill: "none", "stroke-width": 3, class: "avatar-ring"});
    const head = el("g", {class: "avatar-head"});
    const ball = el("circle", {cx: 60, cy: 60, r: 46, fill: `url(#${id})`});
    const shine = el("ellipse", {cx: 44, cy: 38, rx: 14, ry: 8, fill: "#fff", opacity: 0.22});
    const face = el("g", {class: "avatar-face"});
    const left = el("rect", {x: 41, y: 47, width: 9, height: 13, rx: 4.5, fill: "#fff"});
    const right = el("rect", {x: 70, y: 47, width: 9, height: 13, rx: 4.5, fill: "#fff"});
    const mouth = el("rect", {x: 51, y: 72, width: 18, height: 4, rx: 2, fill: "#fff", opacity: 0.92});
    face.append(left, right, mouth);
    head.append(ball, shine, face);
    const snore = el("text", {x: 88, y: 30, "font-size": 16, "font-weight": 700, fill: "#fff", opacity: 0, class: "avatar-z"});
    snore.textContent = "z";
    svg.append(defs, ring, head, snore);
    container.replaceChildren(svg);

    let state = "idle";
    let level = 0;
    let shown = 0;
    let blinkAt = performance.now() + 2500;
    let running = true;
    const levelOf = {speaking: null, listening: null};  // Functions returning the live level.

    function paint(pair) {
      light.setAttribute("stop-color", pair[1]);
      deep.setAttribute("stop-color", pair[0]);
      ring.setAttribute("stroke", pair[0]);
    }
    paint(colors);

    function frame(now) {
      if (!running) return;
      if (!svg.isConnected || !svg.getClientRects().length) {  // Hidden: skip the drawing.
        requestAnimationFrame(frame);
        return;
      }
      const t = now / 1000;
      let target = level;
      if (state === "speaking" && levelOf.speaking) target = levelOf.speaking();
      else if ((state === "hearing" || state === "listening") && levelOf.listening) target = levelOf.listening();
      else if (state === "speaking") target = 0.35 + 0.25 * Math.abs(Math.sin(t * 7.3)) * Math.abs(Math.sin(t * 3.1));
      shown += (target - shown) * 0.35;

      const asleep = state === "sleeping";
      // Breathing (slow when asleep), and a lean when speaking.
      const breathe = state === "speaking" ? 1 + shown * 0.05 : 1 + Math.sin(t * (asleep ? 0.8 : 1.6)) * (asleep ? 0.02 : 0.012);
      head.setAttribute("transform", `translate(60 60) scale(${breathe.toFixed(4)}) translate(-60 -60)`);
      // Mouth: open with the voice; a small smile otherwise.
      const open = state === "speaking" ? 4 + shown * 16 : state === "thinking" || state === "working" || asleep ? 3 : 4;
      mouth.setAttribute("height", open.toFixed(2));
      mouth.setAttribute("y", (74 - open / 2).toFixed(2));
      mouth.setAttribute("width", state === "speaking" ? (16 + shown * 6).toFixed(2) : "18");
      mouth.setAttribute("x", state === "speaking" ? (60 - (16 + shown * 6) / 2).toFixed(2) : "51");
      // Eyes: look up and to the side while thinking; blink every few seconds.
      const look = state === "thinking" || state === "working" || state === "connecting"
        ? `translate(${(3 + Math.sin(t * 2) * 1.5).toFixed(2)} -4)` : "translate(0 0)";
      face.setAttribute("transform", look);
      let eye = asleep ? 2 : 13;
      snore.setAttribute("opacity", asleep ? (0.35 + 0.35 * Math.sin(t * 1.2)).toFixed(2) : "0");
      snore.setAttribute("y", asleep ? (30 - ((t * 6) % 14)).toFixed(1) : "30");
      if (!asleep && now > blinkAt) {
        const phase = (now - blinkAt) / 140;
        eye = phase < 1 ? 13 * (1 - phase) + 1.5 * phase : phase < 2 ? 1.5 + 11.5 * (phase - 1) : 13;
        if (phase >= 2) blinkAt = now + 2200 + Math.random() * 3800;
      }
      for (const e of [left, right]) {
        e.setAttribute("height", eye.toFixed(2));
        e.setAttribute("y", (53.5 - eye / 2).toFixed(2));
      }
      // The ring follows your voice while Bridge listens.
      const hearing = state === "hearing" || state === "listening";
      const grow = hearing ? 1 + shown * 0.12 : state === "speaking" ? 1 + shown * 0.06 : 1;
      ring.setAttribute("transform", `translate(60 60) scale(${grow.toFixed(4)}) translate(-60 -60)`);
      ring.setAttribute("opacity", hearing ? (0.25 + shown * 0.6).toFixed(2) : state === "speaking" ? "0.25" : "0.12");
      svg.classList.toggle("dim", state === "muted" || state === "ended" || state === "failed");
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    return {
      setState(next) { state = next; },
      setLevel(value) { level = value; },
      setColors(pair) { paint(pair); },
      listenTo(kind, getLevel) { levelOf[kind] = getLevel; },
      destroy() { running = false; },
    };
  }

  return {create, meter};
})();
