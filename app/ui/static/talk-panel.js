"use strict";

// The Mac's talk panel: opened by "Hey Bridge" or ⌘⇧Space when Conversation mode is on. It
// stays open while you work in other apps, and closes itself when the conversation ends.
(() => {
  const $ = (id) => document.getElementById(id);
  const mini = window.BridgeMini;
  const talk = window.BridgeTalk;
  talk.mount($("t-talk"));

  function begin() {
    if (talk.active()) return;
    talk.start({
      request: mini.request,
      root: $("t-talk"),
      onEnd: (message) => {
        mini.native({action: "talk_ended"});
        setTimeout(() => { if (!talk.active()) mini.native({action: "hide"}); }, message ? 4500 : 900);
      },
    });
  }

  $("t-close").addEventListener("click", () => (talk.active() ? talk.stop() : mini.native({action: "hide"})));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") (talk.active() ? talk.stop() : mini.native({action: "hide"}));
  });

  window.BridgeTalkPanel = {start: () => mini.ready.then(begin), stop: () => talk.stop(), active: () => talk.active()};
  if (mini.opened.includes("start=1")) mini.ready.then(begin);
})();
