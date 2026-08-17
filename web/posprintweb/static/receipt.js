"use strict";

// The receipt renderer, shared by the print preview and the gallery.
//
// It lives here rather than in app.js because both pages must draw a message
// identically - the gallery's whole promise is "this is what came out". wrap()
// in particular was built to match Python's textwrap line for line, verified
// against real textwrap output; a second copy would drift the first time
// either was touched and the two pages would quietly disagree.
//
// Plain globals, no modules: these pages are three files served from one
// directory and a build step would be the most complicated thing here.

const Receipt = (() => {
  const BRAILLE = /[⠀-⣿]/;

  // Mirrors the wrapping posprint does at print time, which is Python's
  // textwrap: runs of spaces survive inside a line, are dropped where a line
  // breaks, and the first line keeps its indent.
  function wrap(text, cols) {
    const out = [];
    for (const para of text.split("\n")) {
      // The overwhelmingly common case, and the one art depends on: it fits,
      // so it goes through untouched, spacing and all.
      if (para.length <= cols) { out.push(para); continue; }

      const chunks = para.split(/(\s+)/).filter((c) => c !== "");
      let line = "";
      let firstLine = true;
      const flush = () => {
        out.push(line.replace(/\s+$/, ""));
        line = "";
        firstLine = false;
      };

      for (const chunk of chunks) {
        const isSpace = /^\s+$/.test(chunk);
        // Whitespace landing at the start of a continuation line is dropped;
        // on the very first line it is indentation and must be kept.
        if (isSpace && line === "" && !firstLine) continue;
        if (line.length + chunk.length <= cols) { line += chunk; continue; }
        if (isSpace) { flush(); continue; }
        if (line !== "") flush();
        let word = chunk;
        while (word.length > cols) {        // a single word longer than the roll
          out.push(word.slice(0, cols));
          word = word.slice(cols);
          firstLine = false;
        }
        line = word;
      }
      if (line !== "") flush();
    }
    return out;
  }

  // Mirrors posprint's encode_text and the server's filters.unprintable: the
  // character itself, then an explicit replacement, then accent folding. The
  // tables come from the server so this cannot drift from the real code page.
  function asPrinted(ch, charset) {
    if (!charset) return ch;
    if (charset.printable.has(ch)) return ch;

    const replacement = charset.replacements[ch];
    if (replacement && [...replacement].every((c) => charset.printable.has(c))) {
      return replacement;
    }
    const folded = ch.normalize("NFKD").replace(/\p{M}/gu, "");
    if (folded && [...folded].every((c) => charset.printable.has(c))) return folded;
    return null;                     // reaches the paper as '?', or not at all
  }

  // The text as it would come off the roll, plus the characters that cannot
  // make it there.
  function toPaper(text, charset) {
    let out = "";
    const bad = [];
    for (const ch of text) {
      if (ch === "\n" || ch === "\t") { out += ch; continue; }
      const printed = asPrinted(ch, charset);
      if (printed === null) {
        out += "?";
        if (!bad.includes(ch)) bad.push(ch);
      } else {
        out += printed;
      }
    }
    return { text: out, bad };
  }

  const pad = (s, cols, unitWidth = 1) =>
    " ".repeat(Math.max(0, Math.floor((cols - s.length * unitWidth) / 2)));
  const centre = (s, cols) => pad(s, cols) + s;
  const rightAlign = (s, cols) => " ".repeat(Math.max(0, cols - s.length)) + s;
  const esc = (s) =>
    s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

  function stamp(date) {
    const p = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ` +
           `${p(date.getHours())}:${p(date.getMinutes())}`;
  }

  /**
   * The whole receipt, as HTML for a <pre>.
   *
   * Returns markup rather than text because the double-width INCOMING heading
   * needs a span to be drawn at the right size. Everything originating from a
   * visitor goes through esc() on the way in - that is the only reason this is
   * safe to assign to innerHTML, and the reason nothing else here should be
   * interpolated without it.
   */
  function render({ message, name, when, cols, charset }) {
    // Braille prints as a decoded picture, not text, so it is neither degraded
    // to the code page nor wrapped - its width is the picture. Same as the
    // print preview does.
    const art = BRAILLE.test(message);
    const body = art ? message : toPaper(message, charset).text;
    const who = (name ? toPaper(name, charset).text : "") || "someone on the internet";
    const lines = art ? body.split("\n") : wrap(body, cols);

    return [
      pad("INCOMING", cols, 2) + `<span class="dbl">INCOMING</span>`,
      esc(centre(stamp(when), cols)),
      "=".repeat(cols),
      ...lines.map(esc),
      "-".repeat(cols),
      esc(rightAlign(`from: ${who}`, cols)),
      "",
    ].join("\n");
  }

  return { BRAILLE, wrap, toPaper, render, pad, centre, rightAlign, esc, stamp };
})();
