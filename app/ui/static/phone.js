"use strict";

// Bridge on your phone. On the Mac: Settings › Phone (Tailscale, phone access, the QR code that
// signs the phone in, signed-in phones). On the phone: the You tab (Face ID, Home Screen,
// notifications) and opening the right screen when a notification is tapped.
(() => {
  const $ = (id) => document.getElementById(id);
  const ui = () => window.BridgeUI;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function button(text, className, onClick) {
    const element = node("button", text, className);
    element.type = "button";
    element.addEventListener("click", onClick);
    return element;
  }

  function link(text, href) {
    const element = node("a", text, "secondary small button-link");
    element.href = href;
    element.target = "_blank";
    element.rel = "noopener";
    return element;
  }

  function row(name, hint, ...controls) {
    const line = node("div", undefined, "set");
    const words = node("div", undefined, "set-words");
    words.append(node("span", name, "set-name"));
    if (hint) words.append(node("span", hint, "set-hint"));
    const end = node("span", undefined, "set-end");
    end.append(...controls.filter(Boolean));
    line.append(words, end);
    return line;
  }

  const pill = (text, tone = "") => node("span", text, `pill-state ${tone}`);

  function ago(seconds) {
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 90) return "just now";
    if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
    return new Date(seconds * 1000).toLocaleDateString([], {day: "numeric", month: "short"});
  }

  async function attempt(task) {
    try {
      return await task();
    } catch (error) {
      ui().notify(error.message, true);
      return null;
    }
  }

  // ---- On the Mac: Settings › Phone ------------------------------------------------------

  let pairTimer = null;
  let pairExpires = 0;
  let pairing = false;
  let busy = false;

  async function showSettings() {
    const data = await attempt(() => ui().request("/api/v1/phone"));
    if (data) renderSettings(data);
  }

  function toggle(label, on, onChange) {
    const control = node("button", undefined, on ? "switch on" : "switch");
    control.type = "button";
    control.setAttribute("role", "switch");
    control.setAttribute("aria-checked", String(on));
    control.setAttribute("aria-label", label);
    control.append(node("span", undefined, "knob"));
    control.addEventListener("click", () => onChange(!on));
    return control;
  }

  async function change(path, body) {
    if (busy) return;
    busy = true;
    const data = await attempt(() => ui().request(path, "POST", body));
    busy = false;
    if (data) renderSettings(data);
    return data;
  }

  function renderSettings(data) {
    const net = data.tailscale;
    const box = $("settings-phone");
    const phone = (net.phones || []).find((item) => item.online) || (net.phones || [])[0];
    const on = data.enabled && data.serving;
    box.replaceChildren(
      row("Tailscale on this Mac",
        !net.installed ? "A free private network between your own devices. Install it, then sign in."
          : net.running ? `Signed in${net.login ? ` as ${net.login}` : ""}.` : "Open Tailscale and sign in.",
        net.running ? pill("Ready", "good") : net.installed ? pill("Not signed in", "warm") : link("Get Tailscale", "https://tailscale.com/download/mac")),
      row("Tailscale on your phone",
        phone ? `${phone.name} is in your Tailscale${phone.online ? "" : " but offline right now"}.`
          : "Install Tailscale on your phone and sign in with the same account.",
        phone ? pill(phone.online ? "Online" : "Offline", phone.online ? "good" : "") : pill("Not found", "warm")),
      row("HTTPS certificates",
        net.https ? "On: your phone gets a private, encrypted address." : "Needed for Face ID and notifications. In Tailscale's admin page, open DNS and turn on HTTPS Certificates.",
        net.https ? pill("On", "good") : link("Open Tailscale DNS", data.admin_url)),
      row("Let my phone use Bridge",
        on ? `On, at ${data.url.replace("https://", "")} — only your devices can reach it.`
          : "Shares Bridge with your own Tailscale devices through Tailscale Serve. Off removes it.",
        toggle("Let my phone use Bridge", on, (next) => {
          if (!next && !window.confirm("Turn off phone access? Your phone is signed out and stops getting notifications.")) return;
          change(next ? "/api/v1/phone/enable" : "/api/v1/phone/disable");
        })),
    );
    if (on) {
      const seg = node("div", undefined, "seg");
      seg.setAttribute("role", "radiogroup");
      seg.setAttribute("aria-label", "Notify my phone");
      for (const [key, text] of [["away", "When I'm away"], ["always", "Always"]]) {
        const option = button(text, key === data.notify ? "on" : "", () => { if (key !== data.notify) change("/api/v1/phone/settings", {notify: key}); });
        option.setAttribute("role", "radio");
        option.setAttribute("aria-checked", String(key === data.notify));
        seg.append(option);
      }
      box.append(row("Notify my phone",
        data.notify === "away" ? "Only when your Mac is locked or you haven't used it for 5 minutes." : "Every notification goes to your phone too.", seg));
    }
    $("phone-pair").hidden = !on;
    if (!on) stopCode();
    else if (!pairTimer) newCode();
    renderDevices(data, on);
  }

  function renderDevices(data, on) {
    const box = $("settings-phone-devices");
    box.hidden = !on || (!data.sessions.length && !data.passkeys.length);
    if (box.hidden) return;
    box.replaceChildren();
    for (const item of data.sessions) {
      box.append(row("Signed-in phone", `Active ${ago(item.last_seen)}. Phone sign-ins last 24 hours, then Face ID.`, null));
    }
    for (const key of data.passkeys) {
      box.append(row(key.name, `Face ID passkey · added ${ago(key.created_at)}${key.last_used ? ` · used ${ago(key.last_used)}` : ""}`,
        button("Remove", "secondary small", async () => {
          await attempt(() => ui().request("/api/v1/auth/passkeys/remove", "POST", {value: key.credential_id}));
          showSettings();
        })));
    }
    box.append(row("Notifications", data.notifications ? `On for ${data.notifications} ${data.notifications === 1 ? "phone app" : "phone apps"}.` : "Not turned on yet — do it from the You tab on your phone.",
      data.sessions.length ? button("Sign out phones", "secondary small", () => change("/api/v1/phone/sign-out")) : null));
  }

  function drawCode(rows) {
    const canvas = $("phone-qr");
    const context = canvas.getContext("2d");
    const size = rows.length;
    const ratio = window.devicePixelRatio || 1;
    const css = 232;
    canvas.width = css * ratio;
    canvas.height = css * ratio;
    canvas.style.width = `${css}px`;
    canvas.style.height = `${css}px`;
    const cell = Math.floor((css * ratio) / (size + 2));
    const offset = Math.floor((css * ratio - cell * size) / 2);
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#111216";
    rows.forEach((line, y) => {
      for (let x = 0; x < line.length; x += 1) if (line[x] === "1") context.fillRect(offset + x * cell, offset + y * cell, cell, cell);
    });
  }

  function stopCode() {
    clearInterval(pairTimer);
    pairTimer = null;
  }

  async function newCode() {
    if (pairing) return;
    stopCode();
    pairing = true;
    const code = await attempt(() => ui().request("/api/v1/phone/pair-code", "POST"));
    pairing = false;
    if (!code) return;
    $("phone-qr").hidden = !code.qr;
    $("phone-pair-link").hidden = Boolean(code.qr);
    if (code.qr) drawCode(code.qr);
    else $("phone-pair-link").textContent = code.url;  // Type or AirDrop it to the phone instead.
    $("phone-qr").title = code.url;
    pairExpires = Date.now() + code.expires_in * 1000;
    const tick = () => {
      const left = Math.round((pairExpires - Date.now()) / 1000);
      if (left <= 0 || $("phone-pair").hidden || $("view-settings").hidden) {
        stopCode();
        $("phone-pair-expiry").textContent = "This code expired.";
        $("phone-qr").classList.add("expired");
        return;
      }
      $("phone-qr").classList.remove("expired");
      $("phone-pair-expiry").textContent = `Works once, for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} more.`;
    };
    tick();
    pairTimer = setInterval(tick, 1000);
  }

  $("phone-pair-new").addEventListener("click", newCode);

  // ---- On the phone: the You tab ------------------------------------------------------------

  const standalone = () => window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  let me = null;

  async function worker() {
    if (!("serviceWorker" in navigator)) return null;
    return navigator.serviceWorker.register("/sw.js");
  }

  async function subscription() {
    const registration = await worker();
    return registration && registration.pushManager ? registration.pushManager.getSubscription() : null;
  }

  async function turnOnNotifications() {
    const registration = await worker();
    if (!registration || !registration.pushManager) throw new Error("This browser can't get notifications from Bridge.");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("Notifications are off for Bridge. Turn them on in Settings › Notifications › Bridge.");
    const current = await registration.pushManager.getSubscription()
      || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: ui().fromB64(me.vapid_public_key)});
    await ui().request("/api/v1/phone/push", "POST", current.toJSON());
    ui().notify("Notifications are on. Bridge will tell you when something needs you.");
  }

  async function addFaceId() {
    const options = await ui().request("/api/v1/auth/passkeys/options", "POST");
    options.challenge = ui().fromB64(options.challenge);
    options.user.id = ui().fromB64(options.user.id);
    options.excludeCredentials = (options.excludeCredentials || []).map((item) => ({...item, id: ui().fromB64(item.id)}));
    let credential;
    try {
      credential = await navigator.credentials.create({publicKey: options});
    } catch (error) {
      throw new Error(error.name === "InvalidStateError" ? "This phone already has Face ID for Bridge." : "Face ID setup was cancelled.");
    }
    await ui().request("/api/v1/auth/passkeys", "POST", {credential: ui().passkeyJSON(credential), name: "Face ID on your phone"});
    ui().notify(standalone() ? "Face ID is set up." : "Face ID is set up. Next: add Bridge to your Home Screen.");
  }

  async function showYou() {
    me = await attempt(() => ui().request("/api/v1/phone/me"));
    if (!me) return;
    const box = $("phone-you");
    const push = "PushManager" in window && "Notification" in window;
    const subscribed = push && Notification.permission === "granted" && await subscription().catch(() => null);
    const act = (text, className, task) => button(text, className, async () => {
      if (await attempt(task) !== null) showYou();
    });
    box.replaceChildren(
      row("Face ID", me.face_id ? "Sign in with Face ID. Phone sign-ins last 24 hours." : "Sign in quickly and safely, without the QR code.",
        me.face_id ? pill("On", "good") : act("Set up", "primary small", async () => { await addFaceId(); return true; })),
      row("Home Screen", standalone() ? "Bridge opens like an app." : "In Safari, tap Share, then Add to Home Screen. Open Bridge from there and sign in with Face ID.",
        standalone() ? pill("Added", "good") : pill("Not yet", "warm")),
      row("Notifications",
        !standalone() ? "Available once Bridge is on your Home Screen."
          : !push ? "Update your phone to get notifications from Bridge."
          : subscribed ? (me.notify === "away" ? "On, when you're away from your Mac." : "On, for every notification.")
          : "Approvals, heads-ups and replies waiting for you.",
        !standalone() || !push ? null
          : subscribed ? act("Send a test", "secondary small", () => ui().request("/api/v1/phone/push/test", "POST"))
          : act("Turn on", "primary small", async () => { await turnOnNotifications(); return true; })),
    );
  }

  async function signOut() {
    const current = await subscription().catch(() => null);
    if (current) {
      await ui().request("/api/v1/phone/push/remove", "POST", {endpoint: current.endpoint}).catch(() => {});
      await current.unsubscribe().catch(() => {});
    }
    await fetch("/api/v1/auth/logout", {method: "POST", credentials: "same-origin"}).catch(() => {});
    window.location.replace("/");
  }

  function signedIn() {
    worker().catch(() => {});
    let paired = false;
    try {
      paired = sessionStorage.getItem("bridge-just-paired") === "1";
      sessionStorage.removeItem("bridge-just-paired");
    } catch {
      paired = false;
    }
    if (paired) {
      ui().view("phone");
      ui().notify("You're signed in. Set up Face ID, then add Bridge to your Home Screen.");
    }
  }

  $("phone-sign-out").addEventListener("click", signOut);

  // A tapped notification opens its screen (the service worker sends it here).
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (event) => {
      const go = event.data && event.data.view;
      if (typeof go === "string" && window.BridgeUI && ui().phone()) ui().view(go);
    });
  }

  // The Inbox count on the tab bar follows the sidebar's.
  const badge = $("inbox-badge");
  if (badge) {
    new MutationObserver(() => {
      $("tab-inbox-badge").hidden = badge.hidden;
      $("tab-inbox-badge").textContent = badge.textContent;
    }).observe(badge, {attributes: true, childList: true, characterData: true, subtree: true});
  }

  window.BridgePhone = {showSettings, showYou, signedIn};
})();
