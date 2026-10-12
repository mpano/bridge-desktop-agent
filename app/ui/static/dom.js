"use strict";

// Small DOM helpers shared by Bridge's pages. Text always goes in as textContent, never HTML.
window.BridgeDOM = (() => {
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function button(label, className, onClick, title) {
    const element = node("button", label, className);
    element.type = "button";
    if (title) {
      element.title = title;
      element.setAttribute("aria-label", title);
    }
    element.addEventListener("click", onClick);
    return element;
  }

  // Links leave Bridge: they open in your browser, without telling the site where you came from.
  function link(text, href, className) {
    const element = node("a", text, className);
    element.href = href;
    element.target = "_blank";
    element.rel = "noopener noreferrer";
    return element;
  }

  // Bridge's line icons (16×16, drawn with the current text color).
  const ICONS = {
    mic: "M8 1.8a2.2 2.2 0 0 0-2.2 2.2v3.5a2.2 2.2 0 0 0 4.4 0V4A2.2 2.2 0 0 0 8 1.8zM4 7.3a4 4 0 0 0 8 0M8 11.3v2.9M5.8 14.2h4.4",
    people: "M6 7.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM1.8 13.5c.4-2.4 2.1-3.8 4.2-3.8s3.8 1.4 4.2 3.8M11 7.2a2 2 0 1 0-.6-3.9M11.6 9.8c1.4.3 2.4 1.5 2.7 3.2",
    compass: "M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM10.6 5.4 9.2 9.2 5.4 10.6l1.4-3.8z",
    question: "M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM6.2 6.2a1.9 1.9 0 1 1 2.5 1.8c-.5.2-.7.6-.7 1.1v.4M8 11.6v.1",
    face: "M8 14.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM5.8 6.5v.6M10.2 6.5v.6M5.4 9.6a3.2 3.2 0 0 0 5.2 0",
    bolt: "M9 1.5 3.5 9H8l-1 5.5L12.5 7H8z",
    bell: "M4 11.5V7a4 4 0 0 1 8 0v4.5l1.2 1.2H2.8zM6.6 13.8a1.5 1.5 0 0 0 2.8 0",
    report: "M4 1.8h5.5L12.5 5v9.2H4zM9.5 1.8V5h3M6 8h4.5M6 10.5h4.5M6 5.5h2",
    wave: "M2 8h1.2M4.6 5.5v5M7 3.5v9M9.4 5.5v5M11.8 7v2M14 8h-1",
  };

  function icon(name, className = "icon") {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", ICONS[name] || ICONS.bolt);
    svg.append(path);
    return svg;
  }

  return {node, button, link, icon};
})();
