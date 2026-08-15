"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  form: $("form"), message: $("message"), name: $("name"),
  submit: $("submit"), count: $("count"), max: $("max"),
  paper: $("paper"), status: $("status"), title: $("title"),
  blurb: $("blurb"), quota: $("quota"), error: $("error"),
  counterLine: $("counter-line"),
  camera: $("camera"), cameraImg: $("camera-img"), cameraNote: $("camera-note"),
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
let brailleCfg = null;
// Braille art that mixes in ordinary text cannot be drawn, so the send is
// blocked for a reason that has nothing to do with the code page.
let mixedArt = false;
// The textarea's maxLength has to clear the largest braille message, which is
// far past max_chars, so it can no longer be the guardrail for ordinary text.
// This is.
let tooLong = false;

/* -- braille art --------------------------------------------------------- */

const BRAILLE = /[⠀-⣿]/;
const BRAILLE_OR_SPACE = /[⠀-⣿\s]/;

// Mirrors braille.py exactly - same detection, same trimming, same integer
// scale - so the estimate shown here is what the server actually does. If one
// side changes, change both.
function scaleFor(cols, rows) {
  let scale = Math.min(Math.floor(brailleCfg.printer_dots / (cols * 2)),
                       brailleCfg.max_scale);
  if (rows * 4 * scale > brailleCfg.max_dots) {
    scale = Math.floor(brailleCfg.max_dots / (rows * 4));
  }
  return Math.max(1, scale);
}

function brailleArt(text) {
  if (!brailleCfg || !brailleCfg.enabled || !BRAILLE.test(text)) return null;

  const stray = [...new Set([...text].filter((c) => !BRAILLE_OR_SPACE.test(c)))];
  const lines = text.split("\n");
  // U+2800 is a blank braille cell, not whitespace, so a padded line counts as
  // content here and in Python alike.
  while (lines.length && !lines[0].trim()) lines.shift();
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  if (!lines.length) return null;

  const rows = lines.length;
  const cols = Math.max(...lines.map((l) => l.length));
  const scale = scaleFor(cols, rows);
  return { stray, rows, cols, scale, mm: (rows * 4 * scale) / 203 * 25.4 };
}

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

// Mirrors the wrapping posprint does at print time, which is Python's textwrap:
// runs of spaces survive inside a line, are dropped where a line breaks, and
// the first line keeps its indent.
//
// The previous version split on /\s+/ and rejoined with single spaces, which
// silently flattened every run. Prose survived that; ASCII art did not, and the
// preview disagreed with the paper for exactly the messages where alignment is
// the entire point.
function wrap(text, cols) {
  const out = [];
  for (const para of text.split("\n")) {
    // The overwhelmingly common case, and the one art depends on: it fits, so
    // it goes through untouched, spacing and all.
    if (para.length <= cols) { out.push(para); continue; }

    const chunks = para.split(/(\s+)/).filter((c) => c !== "");
    let line = "";
    let firstLine = true;
    const flush = () => { out.push(line.replace(/\s+$/, "")); line = ""; firstLine = false; };

    for (const chunk of chunks) {
      const isSpace = /^\s+$/.test(chunk);
      // Whitespace that lands at the start of a continuation line is dropped;
      // on the very first line it is indentation and must be kept.
      if (isSpace && line === "" && !firstLine) continue;
      if (line.length + chunk.length <= cols) { line += chunk; continue; }
      if (isSpace) { flush(); continue; }
      if (line !== "") flush();
      let word = chunk;
      while (word.length > cols) {          // a single word longer than the roll
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
  // Mirrors filters.clean(): trailing spaces go, blank lines top and bottom go,
  // indentation stays. A .trim() here would re-introduce the exact bug this
  // preview exists to catch.
  const typed = el.message.value.replace(/[ \t]+$/gm, "").replace(/^\n+|\n+$/g, "");
  const from = el.name.value.trim();

  // Preview what the printer will produce, not what the browser can display.
  // A browser has a font for every script; the printer has one code page, and
  // showing the raw text here would promise something the paper cannot keep.
  // Braille is the one thing with no glyphs that still prints perfectly: the
  // server decodes it back into the bitmap it encodes and sends it as an
  // image. So it must skip the charset check that refuses Korean and emoji,
  // and it must not be wrapped - the art's width is the picture.
  const art = brailleArt(typed);
  const message = art ? { text: typed, bad: [] } : toPaper(typed);
  const sender = toPaper(from);
  unprintable = [...new Set([...message.bad, ...sender.bad])];

  const body = message.text.trim() ? message.text : "…";
  const who = sender.text || "someone on the internet";

  // Art is never wrapped: its width *is* the picture, and the server scales
  // the whole bitmap to the head rather than breaking lines.
  const rendered = art ? body.split("\n").map(esc) : wrap(body, cols).map(esc);

  const parts = [
    pad("INCOMING", cols, 2) + `<span class="dbl">INCOMING</span>`,
    esc(centre(stamp(), cols)),
    "=".repeat(cols),
    ...rendered,
    "-".repeat(cols),
    esc(rightAlign(`from: ${who}`, cols)),
    "",
  ];
  el.paper.innerHTML = parts.join("\n");
  // Counted on the cleaned text, which is what check_message() measures.
  tooLong = !art && [...typed].length > limits.max_chars;
  updateNotices(art);
}

// Say what the server will say, before the send, rather than spending the
// visitor's cooldown on a rejection they could have seen coming.
function updateNotices(art) {
  mixedArt = false;

  if (art && art.stray.length) {
    mixedArt = true;
    showError(
      `Braille art prints as a picture, so it has to be on its own - text ` +
      `cannot be drawn into it. Remove: ${art.stray.slice(0, 6).join(" ")}`
    );
    el.error.dataset.reason = "charset";
  } else if (art && art.cols > brailleCfg.max_cols) {
    mixedArt = true;
    showError(`That art is ${art.cols} characters wide; the limit is ` +
              `${brailleCfg.max_cols}.`);
    el.error.dataset.reason = "charset";
  } else if (art && art.rows > brailleCfg.max_rows) {
    mixedArt = true;
    showError(`That art is ${art.rows} lines tall; the limit is ` +
              `${brailleCfg.max_rows}.`);
    el.error.dataset.reason = "charset";
  } else if (art) {
    showNote(`This prints as a picture: ${art.cols}×${art.rows} cells at ` +
             `${art.scale}×, about ${Math.round(art.mm)}mm of paper.`);
  } else if (tooLong) {
    showError(`Too long: ${[...el.message.value].length} characters, the ` +
              `limit is ${limits.max_chars}.`);
    el.error.dataset.reason = "charset";
  } else if (unprintable.length) {
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
  // max_chars measures text. Braille art is bounded by its grid instead, so
  // flagging it here would show a limit that does not apply to it.
  const art = brailleArt(el.message.value);
  el.count.parentElement.classList.toggle("over", !art && n > limits.max_chars);
}

/* -- live camera --------------------------------------------------------- */

// The <img> holds one long-lived multipart connection, so it is attached once
// and left alone. Re-assigning src on every status poll would tear down the
// stream and reconnect twice a minute for no reason.
let cameraAttached = false;

function updateCamera(state) {
  const live = !!(state && state.live);
  el.camera.hidden = !live;

  if (!live) {
    if (cameraAttached) {
      // Dropping src closes the connection; without this the server keeps
      // streaming to a hidden element and counts a viewer that left.
      el.cameraImg.removeAttribute("src");
      cameraAttached = false;
    }
    return;
  }
  if (cameraAttached) return;

  el.cameraImg.src = "/api/camera.mjpg?t=" + Date.now();
  cameraAttached = true;
  el.cameraNote.textContent = state.mode === "after_print"
    ? "Live for a minute or so after each print."
    : "Live.";

  el.cameraImg.onerror = () => {
    // Usually the viewer cap, sometimes ffmpeg dying. Fall back to a still so
    // the section shows something rather than a broken-image icon.
    cameraAttached = false;
    el.cameraImg.src = "/api/camera.jpg?t=" + Date.now();
    el.cameraNote.textContent = "The live view is busy — showing a snapshot.";
  };
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
  el.error.classList.remove("error--info");
  // Tagged so the live charset warning knows which messages are its own to
  // withdraw, and does not wipe a print result the visitor is still reading.
  delete el.error.dataset.reason;
}

function clearError() {
  el.error.hidden = true;
  delete el.error.dataset.reason;
}

// Neutral, not a failure: the message is fine and this says what will happen.
function showNote(msg) {
  el.error.hidden = false;
  el.error.textContent = msg;
  el.error.classList.remove("error--ok");
  el.error.classList.add("error--info");
  el.error.dataset.reason = "charset";
}

function syncSubmit() {
  // A cooldown owns the button's label as well as its state, so it is left to
  // startCooldown's ticker rather than being fought over here.
  if (cooldownUntil > Date.now()) return;
  el.submit.disabled = printerBlocked || unprintable.length > 0 || mixedArt || tooLong;
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
    if (s.braille) brailleCfg = s.braille;
    updateCamera(s.camera);
    el.title.textContent = s.title;
    document.title = s.title;
    el.blurb.textContent = s.blurb;
    // maxLength has to clear the largest *braille* message, not the largest
    // text one, or art cannot even be pasted into the box. Text over max_chars
    // still turns the counter red and is refused server-side.
    el.message.maxLength = brailleCfg && brailleCfg.enabled
      ? Math.max(limits.max_chars,
                 brailleCfg.max_cols * brailleCfg.max_rows + brailleCfg.max_rows)
      : limits.max_chars;
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
      setStatus("paper", "The printer is out of paper. Nothing can print " +
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
