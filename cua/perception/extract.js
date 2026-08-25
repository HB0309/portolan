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
   * sits, when the markup does not say.
   *
   * Two table shapes need two different answers, and neither this app nor
   * legacy markup in general marks the difference with a class name or a
   * semantic <th> -- both a label/value form row and a data-grid header row
   * are plain <td>s here. The shapes have to be told apart structurally:
   *
   *   - A label/value pair: exactly two cells per row (caption, value),
   *     repeated down the form. The cell to the left IS the label.
   *   - A data grid: more than two cells per row (a real column set --
   *     Type, Account No., Balance, Opened...). The cell to the left is
   *     another data value, never a caption; the label is the header cell
   *     directly above, at the same column position.
   *
   * Found live: a capability that read an account's balance from a data
   * grid recorded the *account number in the next cell over* as the
   * balance's identity, because the left-neighbour walk below ran
   * unconditionally and "succeeded" on the wrong neighbour before anything
   * got a chance to notice the row had four columns, not two. It replayed
   * fine for the member it was recorded against and failed outright for
   * every other member, since account numbers are unique per record.
   *
   * The >2-cells rule is a heuristic, not a proof -- a genuine two-column
   * grid (just "Name | Balance", nothing else) would still be misread as a
   * label/value pair, the same class of edge case the irreversible-action
   * name matcher already accepts elsewhere in this project. It is right for
   * the common legacy shapes (paired-row forms, header-plus-columns grids),
   * which is what matters here, not universal correctness on every table
   * shape anyone could author.
   *
   * One correction found immediately by actually running it: cell *count*
   * alone over-counts a row that carries a rowspan cell for something else
   * entirely -- this app's search form is a two-cell label/value row plus a
   * submit button that spans three rows down the side, which is three
   * children, not two, and was misread as a data grid. A rowspan cell is
   * never a repeating column; only cells that start fresh on this row (no
   * rowspan, or rowspan of 1) count toward "how wide is this row" here.
   */
  const rowWidth = (row) =>
    Array.prototype.filter.call(row.children, (c) => (c.rowSpan || 1) <= 1).length;

  const labelHint = (el) => {
    const cell = el.tagName.toLowerCase() === "td" ? el : el.closest("td");
    if (!cell) return "";
    const row = cell.closest("tr");
    const isDataGrid = row && rowWidth(row) > 2;

    if (!isDataGrid) {
      let prev = cell.previousElementSibling;
      while (prev) {
        const t = text(prev);
        if (t && t.length <= 60) return t.replace(/[:*]\s*$/, "");
        prev = prev.previousElementSibling;
      }
    }

    /* A data-grid cell's label is the header, at the same column position --
     * but "the row above" is only the header for the first data row. Found
     * live, immediately: for the second account in a two-account list, the
     * row above is the *first account's* row, not the header, and it has
     * the same width, so the previous version of this happily read that
     * row's Balance cell instead. The header is only ever the table's own
     * first row, however many data rows deep the current one is -- walking
     * up one sibling at a time was the bug, not a detail to preserve. */
    if (isDataGrid) {
      const table = row.closest("table");
      const header = table && table.rows.length ? table.rows[0] : null;
      if (header && header !== row && header.children.length === row.children.length) {
        const index = Array.prototype.indexOf.call(row.children, cell);
        const t = text(header.children[index]);
        if (t && t.length <= 60) return t.replace(/[:*]\s*$/, "");
      }
    }

    /* A control embedded in a themed grid cell whose own row had no usable
     * left-neighbour caption: the label is directly above it, one row up.
     * Unlike the data-grid case this is a single header-plus-controls row,
     * not repeating data, so "the row above" is exactly right here. */
    if (!(el.tagName.toLowerCase() === "td" || el.tagName.toLowerCase() === "th")) {
      const previousRow = row ? row.previousElementSibling : null;
      if (previousRow && previousRow.children.length === row.children.length) {
        const index = Array.prototype.indexOf.call(row.children, cell);
        const header = previousRow.children[index];
        const t = text(header);
        if (t && t.length <= 60) return t.replace(/[:*]\s*$/, "");
      }
    }
    return "";
  };

  /* Which panel of the screen is this control in?
   *
   * Sections are what let a descriptor say "the Search button in Member Search"
   * rather than "one of the four Search buttons", and they are what a generated
   * checkpoint asserts on. Getting them wrong is expensive in a way that is not
   * obvious: with no section, a checkpoint marker falls back to whatever text is
   * around, which on an application with a menu bar and a toolbar means chrome
   * that appears on every page -- an assertion that passes everywhere.
   *
   * The cues are tried structural-first, because markup that means "this names
   * the group" is portable, while a class name is one app's convention. Class
   * names are still consulted last, since legacy markup often has nothing else.
   */
  const sectionOf = (el) => {
    const fieldset = el.closest ? el.closest("fieldset") : null;
    if (fieldset) {
      const legend = text(fieldset.querySelector("legend"));
      if (legend && legend.length <= 80) return legend;
    }

    let node = el;
    while (node && node !== document.body) {
      const table = node.closest ? node.closest("table") : null;
      if (!table) break;
      const header = table.querySelector("caption, th, .caption, .panelhdr");
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

  /* The other cells in this data-grid row, for telling one row apart from
   * another when a table has more than one -- e.g. a member with both a
   * Savings and a Checking account, where the Balance column alone reads
   * identically in both rows and a recorded capability that only knows
   * "the Balance cell" cannot say which one it meant.
   *
   * Not every cell qualifies: only genuine multi-column data-grid rows have
   * peers worth naming (a label/value form's "cell to the left" is already
   * the label itself, handled by labelHint above). Column *order* varies by
   * tenant (a reversed-columns variant puts Balance first instead of
   * third), so this returns every peer's text rather than assuming which
   * position holds the identifying one -- the recorder picks the one that
   * looks like a category, not a value, from whichever position it's in. */
  const rowPeersOf = (el) => {
    const cell = el.tagName && el.tagName.toLowerCase() === "td" ? el : (el.closest ? el.closest("td") : null);
    if (!cell) return [];
    const row = cell.closest("tr");
    if (!row || rowWidth(row) <= 2) return [];
    return Array.prototype.map
      .call(row.children, (c) => (c === cell ? null : text(c)))
      .filter((t) => t);
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
      row_peers: role === "cell" || role === "columnheader" ? rowPeersOf(el) : [],
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
