"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  form: $("form"), message: $("message"), name: $("name"),
  submit: $("submit"), count: $("count"), max: $("max"),
  paper: $("paper"), status: $("status"), title: $("title"),
  blurb: $("blurb"), quota: $("quota"), error: $("error"),
  counterLine: $("counter-line"),
  camera: $("camera"), cameraImg: $("camera-img"), cameraNote: $("camera-note"),
  cameraFrame: $("camera-frame"),
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

const BRAILLE = Receipt.BRAILLE;
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

  // Each braille codepoint carries eight dots, so its set bits *are* its ink.
  // Same arithmetic as braille.ink_fraction() on the server, exactly.
  let dots = 0, cells = 0;
  for (const ch of text) {
    const bits = ch.codePointAt(0) - 0x2800;
    if (bits >= 0 && bits <= 0xff) {
      cells += 1;
      for (let b = bits; b; b >>= 1) dots += b & 1;
    }
  }
  const ink = cells ? dots / (cells * 8) : 0;

  return { stray, rows, cols, scale, ink, mm: (rows * 4 * scale) / 203 * 25.4 };
}

/* -- receipt rendering --------------------------------------------------- */
//
// wrap(), toPaper() and the alignment helpers moved to receipt.js so the
// gallery draws a message exactly as this preview does. Thin wrappers here
// keep the call sites below unchanged and bind the charset once.

const toPaper = (text) => Receipt.toPaper(text, charset);

// Mirrors filters.clean() on the server: trailing spaces go, blank lines
// top and bottom go, indentation stays.
//
// Used by BOTH the preview and the send. It lived only in the preview while
// the send still called .trim(), so a drawing looked right on screen and
// arrived with its first line shoved left - the exact bug the preview exists
// to catch, hiding in the gap between the two copies.
function asTyped(text) {
  return text.replace(/[ \t]+$/gm, "").replace(/^\n+|\n+$/g, "");
}

function renderPreview() {
  const cols = limits.columns;
  const typed = asTyped(el.message.value);
  const from = el.name.value.trim();

  // Preview what the printer will produce, not what the browser can display.
  // A browser has a font for every script; the printer has one code page, and
  // showing the raw text here would promise something the paper cannot keep.
  // Braille is the one thing with no glyphs that still prints perfectly: the
  // server decodes it back into the bitmap it encodes and sends it as an
  // image. So it must skip the charset check that refuses Korean and emoji,
  // and it must not be wrapped - the art's width is the picture.
  const art = brailleArt(typed);
  // The charset warning still needs to know which characters would be lost,
  // which Receipt.render does not report - so the conversion runs here too.
  // It is pure and cheap, and the alternative is a second return value that
  // only one of the two callers wants.
  unprintable = art
    ? [...new Set(toPaper(from).bad)]
    : [...new Set([...toPaper(typed).bad, ...toPaper(from).bad])];

  el.paper.innerHTML = Receipt.render({
    message: typed.trim() ? typed : "…",
    name: from,
    when: new Date(),
    cols,
    charset,
  });
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
  } else if (art && art.ink * 100 > brailleCfg.max_ink) {
    mixedArt = true;
    showError(
      `That picture is ${Math.round(art.ink * 100)}% solid black and the ` +
      `limit is ${brailleCfg.max_ink}%. The printer makes black by heating ` +
      `the paper, so a filled-in image runs hot. Try something more like ` +
      `line art.`
    );
    el.error.dataset.reason = "charset";
  } else if (art) {
    showNote(`This prints as a picture: ${art.cols}×${art.rows} cells at ` +
             `${art.scale}×, about ${Math.round(art.mm)}mm of paper, ` +
             `${Math.round(art.ink * 100)}% ink.`);
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
let cameraRetry = null;
let cameraFailures = 0;

function attachCamera() {
  clearTimeout(cameraRetry);
  cameraRetry = null;
  el.cameraImg.src = "/api/camera.mjpg?t=" + Date.now();
  cameraAttached = true;
}

function detachCamera() {
  clearTimeout(cameraRetry);
  cameraRetry = null;
  // Dropping src closes the connection. Without this the server keeps
  // streaming to an element nobody is looking at, and counts a viewer that has
  // already left against the cap.
  el.cameraImg.removeAttribute("src");
  cameraAttached = false;
  // Reset, so reappearing later does not start with a stale complaint.
  cameraFailures = 0;
  el.cameraFrame.hidden = false;
  el.cameraNote.textContent = "";
}

function updateCamera(state) {
  const live = !!(state && state.live);
  el.camera.hidden = !live;
  if (!live) { detachCamera(); return; }
  // A pending retry owns reattachment. Without this check the status poll
  // would jump the queue every 60s and the backoff would never be honoured.
  if (!cameraAttached && cameraRetry === null) attachCamera();
}

// A picture that is working needs no caption saying so. The note is only for
// when there is nothing to look at.
el.cameraImg.addEventListener("load", () => {
  cameraFailures = 0;
  el.cameraFrame.hidden = false;
  el.cameraNote.textContent = "";
});

el.cameraImg.addEventListener("error", () => {
  cameraAttached = false;
  el.cameraFrame.hidden = true;
  el.cameraNote.textContent = cameraFailures
    ? "Still can't reach the camera."
    : "The camera is unreachable right now.";

  // Back off rather than hammering a camera that is already not answering.
  cameraFailures += 1;
  const delay = Math.min(30000, 2000 * 2 ** (cameraFailures - 1));
  cameraRetry = setTimeout(() => {
    cameraRetry = null;
    if (!el.camera.hidden) attachCamera();
  }, delay);
});

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

  const message = asTyped(el.message.value);
  if (!message.trim()) { showError("Nothing to print."); return; }

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
