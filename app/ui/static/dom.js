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

  return {node, button, link};
})();
