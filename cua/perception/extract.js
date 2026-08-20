/*
 * In-page perception.
 *
 * Produces an accessibility-shaped view of one document: for every element that
 * a human operator could perceive or act on, a role, an accessible name, and
 * the contextual anchors that make a generic name resolvable.
 *
 * Two things here are not standard accessibility and exist specifically because
 * of what legacy enterprise markup looks like:
 *
 *   labelHint  -- these applications do not use <label for>. A field is labelled
 *                 by the table cell to its left, and nothing else. Without this,
 *                 an entire class of input has no name at all and is
 *                 unaddressable by any semantic strategy.
 *
 *   section / rowText -- a page with four controls named "Search" needs
 *                 something to disambiguate them. The enclosing panel title and
 *                 the containing table row are the two anchors that survive
 *                 re-branding, so they are captured for every element.
 *
 * Each element is tagged with a data-cua-ref attribute so the surface can act on
 * it again. The ref is valid only for the observation that produced it; it is
 * never written into an artifact.
 */
(() => {
  const MAX_ELEMENTS = 220;

  const text = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : "generic";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "img") return "img";
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "hidden") return "hidden";
      if (type === "password") return "textbox";
      return "textbox";
    }
    return "generic";
  };

  /* A pragmatic subset of the accessible-name computation: the branches that
   * actually occur in this class of application, in specification order. */
  const accessibleName = (el) => {
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel.trim();

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy
        .split(/\s+/)
        .map((id) => text(document.getElementById(id)))
        .filter(Boolean);
      if (parts.length) return parts.join(" ");
    }

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();

    if (tag === "input" && ["submit", "button", "reset"].includes(type)) {
      return (el.getAttribute("value") || "").trim();
    }
    if (tag === "button" || tag === "a" || /^h[1-6]$/.test(tag)) {
      return text(el);
    }
    if (tag === "img") return (el.getAttribute("alt") || "").trim();

    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return text(label);
    }
    const wrapping = el.closest("label");
    if (wrapping) return text(wrapping);

    const title = el.getAttribute("title");
    if (title) return title.trim();

    if (tag === "td" || tag === "th") return text(el);

    return "";
  };

  /* The legacy affordance: what is this control called, according to where it
   * sits, when the markup does not say. */
  const labelHint = (el) => {
    const cell = el.tagName.toLowerCase() === "td" ? el : el.closest("td");
    if (!cell) return "";
    let prev = cell.previousElementSibling;
    while (prev) {
      const t = text(prev);
      if (t && t.length <= 60) return t.replace(/[:*]\s*$/, "");
      prev = prev.previousElementSibling;
    }
    /* Some forms put the label in the row above rather than the cell to the
     * left, so a grid header can name the control beneath it. This only applies
     * to controls: for a plain data cell in a label/value table, the row above
     * is the *previous field*, not this one's label, and using it would attach
     * confidently wrong identifiers to half the page. */
    if (el.tagName.toLowerCase() === "td" || el.tagName.toLowerCase() === "th") return "";
    const row = cell.closest("tr");
    const previousRow = row ? row.previousElementSibling : null;
    if (previousRow && previousRow.children.length === row.children.length) {
      const index = Array.prototype.indexOf.call(row.children, cell);
      const header = previousRow.children[index];
      const t = text(header);
      if (t && t.length <= 60) return t.replace(/[:*]\s*$/, "");
    }
    return "";
  };

  /* Nearest panel title or heading above this element. */
  const sectionOf = (el) => {
    let node = el;
    while (node && node !== document.body) {
      const table = node.closest ? node.closest("table") : null;
      if (!table) break;
      const header = table.querySelector(".panelhdr, th, caption");
      const t = text(header);
      if (t && t.length <= 80) return t;
      node = table.parentElement;
    }
    let sibling = el.previousElementSibling;
    while (sibling) {
      if (/^h[1-6]$/i.test(sibling.tagName)) return text(sibling);
      sibling = sibling.previousElementSibling;
    }
    return "";
  };

  const rowTextOf = (el) => {
    const row = el.closest ? el.closest("tr") : null;
    return row ? text(row).slice(0, 160) : "";
  };

  const SELECTOR = [
    "a[href]",
    "button",
    "input",
    "select",
    "textarea",
    "td",
    "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "[role]",
  ].join(",");

  const out = [];
  let index = 0;

  document.querySelectorAll("[data-cua-ref]").forEach((el) => el.removeAttribute("data-cua-ref"));

  for (const el of document.querySelectorAll(SELECTOR)) {
    if (out.length >= MAX_ELEMENTS) break;

    const role = roleOf(el);
    if (role === "hidden" || role === "generic") continue;
    if (!isVisible(el)) continue;

    const name = accessibleName(el);

    /* For a form control the label hint stands in for a missing name, so it is
     * only computed when there is nothing better.
     *
     * For a data cell the two are not alternatives: the cell's own text is the
     * *value* ("$4,102.55") and the neighbouring cell is the *identifier*
     * ("Current Savings Balance"). A balance is only ever addressable by its
     * label, so cells always get the hint computed, name or no name. */
    const isCell = role === "cell" || role === "columnheader";
    const hint = (name && !isCell) ? "" : labelHint(el);

    if (role === "cell" || role === "columnheader") {
      /* A cell with neither a name nor a label hint is layout scaffolding.
       * There are hundreds of those on a table-laid-out page and none of them
       * are addressable, so they are noise in the observation the model reads.
       */
      if (!name && !hint) continue;

      /* A cell that contains a nested table or a form control is a container,
       * not a value. Its "name" is the concatenated text of everything inside
       * it, which is both useless as an identifier and expensive in tokens --
       * on a table-laid-out page the outermost cell's name is the whole screen.
       * The things worth perceiving inside it are emitted separately. */
      if (el.querySelector("table, input, select, textarea, button, a")) continue;
    }

    const ref = `e${index++}`;
    el.setAttribute("data-cua-ref", ref);
    const rect = el.getBoundingClientRect();

    out.push({
      ref,
      role,
      name,
      label_hint: hint,
      value: el.value !== undefined && el.value !== null ? String(el.value) : "",
      enabled: !el.disabled,
      visible: true,
      focused: document.activeElement === el,
      section: sectionOf(el),
      row_text: role === "cell" || role === "columnheader" ? rowTextOf(el) : "",
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      attributes: {
        tag: el.tagName.toLowerCase(),
        type: (el.getAttribute("type") || "").toLowerCase(),
        name_attr: el.getAttribute("name") || "",
        href: el.getAttribute("href") || "",
      },
    });
  }

  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : "").replace(/\n{3,}/g, "\n\n").trim(),
    elements: out,
  };
})();
