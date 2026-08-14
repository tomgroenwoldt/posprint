"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  form: $("form"), message: $("message"), name: $("name"),
  submit: $("submit"), count: $("count"), max: $("max"),
  paper: $("paper"), status: $("status"), title: $("title"),
  blurb: $("blurb"), quota: $("quota"), error: $("error"),
  counterLine: $("counter-line"),
};

let limits = { max_chars: 500, max_lines: 20, columns: 48, cooldown_seconds: 60,
               per_ip_daily: 5 };
let cooldownTimer = null;
// The 60s status poll must not re-enable the button underneath a running
// cooldown, so it checks this before touching `disabled`.
let cooldownUntil = 0;
// Same for a message the printer has no glyphs for: the poll must leave the
// button alone until the text is fixed.
let unprintable = [];
// Offline, switched off, or asleep. Three independent reasons to keep the
// button down; inferring them from `disabled` itself means whichever check
// runs last wins, so they are tracked separately and combined in one place.
let printerBlocked = false;

// Filled from /api/status. Until it arrives the preview shows text verbatim,
// which is the pre-existing behaviour and no worse than before.
let charset = null;

/* -- what the printer can actually render -------------------------------- */

// Mirrors posprint's encode_text and the server's filters.unprintable: the
// character itself, then an explicit replacement, then accent folding. The
// tables come from the server so this cannot drift from the real code page.
function asPrinted(ch) {
  if (!charset) return ch;
  if (charset.printable.has(ch)) return ch;

  const replacement = charset.replacements[ch];
  if (replacement && [...replacement].every((c) => charset.printable.has(c))) {
    return replacement;
  }

  const folded = ch.normalize("NFKD").replace(/\p{M}/gu, "");
  if (folded && [...folded].every((c) => charset.printable.has(c))) return folded;

  return null;                       // reaches the paper as '?', or not at all
}

// Returns the text as it would come off the roll, and the distinct characters
// that cannot make it there.
function toPaper(text) {
  let out = "";
  const bad = [];
  for (const ch of text) {
    if (ch === "\n" || ch === "\t") { out += ch; continue; }
    const printed = asPrinted(ch);
    if (printed === null) {
      out += "?";
      if (!bad.includes(ch)) bad.push(ch);
    } else {
      out += printed;
    }
  }
  return { text: out, bad };
}

/* -- receipt preview ----------------------------------------------------- */

// Mirrors the wrapping posprint does at print time. It will not be byte-exact
// for characters the printer's codepage lacks, but it gets the line breaks
// right, which is the part people care about.
function wrap(text, cols) {
  const out = [];
  for (const para of text.split("\n")) {
    if (!para) { out.push(""); continue; }
    let line = "";
    for (const word of para.split(/\s+/)) {
      if (!word) continue;
      if (!line) {
        line = word;
      } else if (line.length + 1 + word.length <= cols) {
        line += " " + word;
      } else {
        out.push(line);
        line = word;
      }
      while (line.length > cols) {          // a single word longer than the roll
        out.push(line.slice(0, cols));
        line = line.slice(cols);
      }
    }
    out.push(line);
  }
  return out;
}

const pad = (s, cols, unitWidth = 1) =>
  " ".repeat(Math.max(0, Math.floor((cols - s.length * unitWidth) / 2)));
const centre = (s, cols) => pad(s, cols) + s;
const rightAlign = (s, cols) => " ".repeat(Math.max(0, cols - s.length)) + s;
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderPreview() {
  const cols = limits.columns;
  const typed = el.message.value.trim();
  const from = el.name.value.trim();

  // Preview what the printer will produce, not what the browser can display.
  // A browser has a font for every script; the printer has one code page, and
  // showing the raw text here would promise something the paper cannot keep.
  const message = toPaper(typed);
  const sender = toPaper(from);
  unprintable = [...new Set([...message.bad, ...sender.bad])];

  const body = message.text || "…";
  const who = sender.text || "someone on the internet";

  const parts = [
    pad("INCOMING", cols, 2) + `<span class="dbl">INCOMING</span>`,
    esc(centre(stamp(), cols)),
    "=".repeat(cols),
    ...wrap(body, cols).map(esc),
    "-".repeat(cols),
    esc(rightAlign(`from: ${who}`, cols)),
    "",
  ];
  el.paper.innerHTML = parts.join("\n");
  showCharsetWarning();
}

// The server refuses these outright, so say so before the send rather than
// spending the visitor's cooldown on a rejection.
function showCharsetWarning() {
  if (unprintable.length) {
    const shown = unprintable.slice(0, 6).join(" ");
    const more = unprintable.length > 6 ? ` (and ${unprintable.length - 6} more)` : "";
    showError(
      `The printer has no glyph for: ${shown}${more}. It only prints Latin ` +
      `letters, digits and punctuation.`
    );
    el.error.dataset.reason = "charset";
  } else if (el.error.dataset.reason === "charset") {
    clearError();
  }
  syncSubmit();
}

function updateCount() {
  const n = [...el.message.value].length;
  el.count.textContent = n;
  el.count.parentElement.classList.toggle("over", n > limits.max_chars);
}

/* -- status -------------------------------------------------------------- */

function setStatus(cls, text) {
  el.status.className = `status status--${cls}`;
  el.status.textContent = text;
}

function showError(msg, ok = false) {
  el.error.hidden = false;
  el.error.textContent = msg;
  el.error.classList.toggle("error--ok", ok);
  // Tagged so the live charset warning knows which messages are its own to
  // withdraw, and does not wipe a print result the visitor is still reading.
  delete el.error.dataset.reason;
}

function clearError() {
  el.error.hidden = true;
  delete el.error.dataset.reason;
}

function syncSubmit() {
  // A cooldown owns the button's label as well as its state, so it is left to
  // startCooldown's ticker rather than being fought over here.
  if (cooldownUntil > Date.now()) return;
  el.submit.disabled = printerBlocked || unprintable.length > 0;
}

function startCooldown(seconds) {
  clearInterval(cooldownTimer);
  cooldownUntil = Date.now() + seconds * 1000;
  let left = seconds;
  const tick = () => {
    if (left <= 0) {
      clearInterval(cooldownTimer);
      cooldownUntil = 0;
      el.submit.textContent = "Print it";
      syncSubmit();
      return;
    }
    el.submit.disabled = true;
    el.submit.textContent = `Wait ${left}s`;
    left -= 1;
  };
  tick();
  cooldownTimer = setInterval(tick, 1000);
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();

    limits = s.limits;
    if (s.charset) {
      charset = {
        printable: new Set(s.charset.printable),
        replacements: s.charset.replacements,
      };
    }
    el.title.textContent = s.title;
    document.title = s.title;
    el.blurb.textContent = s.blurb;
    el.message.maxLength = limits.max_chars;
    el.name.maxLength = limits.max_name_chars;
    el.max.textContent = limits.max_chars;

    printerBlocked = true;
    if (s.disabled) {
      setStatus("offline", "Printing is switched off right now.");
    } else if (s.quiet) {
      setStatus("asleep",
        `Asleep until ${String(s.quiet_hours.end).padStart(2, "0")}:00 — ` +
        `it is ${s.local_time} there.`);
    } else if (s.printer_state === "out_of_paper") {
      setStatus("offline", "The printer is out of paper. Nothing can print " +
        "until someone changes the roll.");
    } else if (!s.online) {
      setStatus("offline", "The printer is offline.");
    } else {
      setStatus("online", "Printer is online.");
      printerBlocked = false;
    }
    syncSubmit();

    el.quota.textContent = s.you.remaining_today > 0
      ? `${s.you.remaining_today} of ${limits.per_ip_daily} prints left today.`
      : "No prints left today.";
    el.counterLine.textContent = `${s.printed_today} messages printed today.`;

    updateCount();
    renderPreview();
  } catch {
    setStatus("offline", "Can't reach the site's backend.");
    printerBlocked = true;
    syncSubmit();
  }
}

/* -- submit -------------------------------------------------------------- */

el.form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  clearError();

  const message = el.message.value.trim();
  if (!message) { showError("Nothing to print."); return; }

  el.submit.disabled = true;
  el.submit.textContent = "Printing…";

  try {
    const r = await fetch("/api/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, name: el.name.value.trim() }),
    });
    const body = await r.json().catch(() => ({}));

    if (r.ok) {
      showError("Printed. It is sitting on my desk right now.", true);
      el.message.value = "";
      updateCount();
      renderPreview();
      startCooldown(body.next_allowed_in || limits.cooldown_seconds);
      el.quota.textContent = `${body.remaining_today} prints left today.`;
    } else {
      showError(body.detail || `Something went wrong (${r.status}).`);
      const retry = parseInt(r.headers.get("Retry-After") || "0", 10);
      if (retry > 0 && retry < 3600) startCooldown(retry);
      else { el.submit.textContent = "Print it"; syncSubmit(); }
    }
  } catch {
    showError("Network error. Is the site still up?");
    el.submit.textContent = "Print it";
    syncSubmit();
  }
});

el.message.addEventListener("input", () => { updateCount(); renderPreview(); });
el.name.addEventListener("input", renderPreview);

refreshStatus();
setInterval(refreshStatus, 60000);
