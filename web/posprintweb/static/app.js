"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  form: $("form"), message: $("message"), name: $("name"),
  submit: $("submit"), count: $("count"), max: $("max"),
  paper: $("paper"), status: $("status"), title: $("title"),
  blurb: $("blurb"), quota: $("quota"), error: $("error"),
  counterLine: $("counter-line"),
  camera: $("camera"), cameraImg: $("camera-img"), cameraNote: $("camera-note"),
  puzzle: $("puzzle"), puzzleImg: $("puzzle-img"), puzzleGrid: $("puzzle-grid"),
  puzzleSkip: $("puzzle-skip"),
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

// The feed is read with fetch and painted frame by frame, rather than pointed
// at with <img src>. An <img> cannot report that a stream has stopped.
//
// Measured in Chrome against a stream ended three ways - closed cleanly, reset
// mid-frame, and simply going silent - the element fires *no event at all* in
// every case: no error, no abort, no stalled. It keeps complete === true, keeps
// its naturalWidth, and goes on showing the last frame it received. Every
// failure was indistinguishable from a working camera, so the error handler
// that was supposed to reconnect could only ever fire before the first frame
// arrived. Once you had a picture, a dead feed stayed dead until a reload.
//
// Reading the body here turns all three into one observable event: no frame
// within CAMERA_TIMEOUT, or the body ending. Either one is a reconnect.

// The server gives up on a stalled producer at 10s and ends the response, so
// in practice the body ends first and this only covers a connection that is
// open but silent - a dropped link, a sleeping laptop, a proxy holding a
// socket nobody is feeding.
const CAMERA_TIMEOUT = 15000;
// Frames are tens of kilobytes. A buffer this size means the boundary scan has
// lost sync, and without a ceiling it would grow until the tab died.
const CAMERA_MAX_BUFFER = 8 * 1024 * 1024;

let cameraAbort = null;          // the live connection, if there is one
let cameraRetry = null;          // a pending reconnect, if there is one
let cameraFailures = 0;

const CRLF2 = new TextEncoder().encode("\r\n\r\n");

function bytesIndexOf(haystack, needle, from) {
  const last = haystack.length - needle.length;
  outer: for (let i = from; i <= last; i += 1) {
    for (let j = 0; j < needle.length; j += 1) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

function concatBytes(a, b) {
  if (a.length === 0) return b;
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

// One frame off the front of the buffer, or null when more bytes are needed.
// Each part carries a Content-Length, so this takes an exact number of bytes
// rather than scanning for the next boundary: a JPEG can contain any byte
// sequence, including one that looks like the boundary.
function takeFrame(buffer, marker) {
  const start = bytesIndexOf(buffer, marker, 0);
  if (start < 0) return null;
  const headEnd = bytesIndexOf(buffer, CRLF2, start + marker.length);
  if (headEnd < 0) return null;

  const head = new TextDecoder("latin1")
    .decode(buffer.subarray(start + marker.length, headEnd));
  const declared = /content-length:\s*(\d+)/i.exec(head);
  if (!declared) throw new Error("frame with no length");

  const from = headEnd + CRLF2.length;
  const to = from + Number(declared[1]);
  if (buffer.length < to) return null;
  return { frame: buffer.slice(from, to), rest: buffer.subarray(to) };
}

function showFrame(bytes) {
  const url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
  const previous = el.cameraImg.src;
  el.cameraImg.src = url;
  // Released as soon as the next frame is assigned: the old one is already
  // decoded and on screen, and keeping them would leak a URL per frame.
  if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);

  if (cameraFailures || el.cameraFrame.hidden) {
    cameraFailures = 0;
    el.cameraFrame.hidden = false;
    el.cameraNote.textContent = "";
  }
}

async function readCamera(signal, onFrame) {
  const res = await fetch("/api/camera.mjpg", { signal, cache: "no-store" });
  if (res.status === 503) throw new Error("busy");
  // 404 is not a failure: it is the killswitch, quiet hours, or after_print
  // closing the window. /api/status is the authority on that, so ask it now
  // rather than complaining for up to a minute until the next poll.
  if (res.status === 404) throw new Error("off");
  if (!res.ok) throw new Error(`feed returned ${res.status}`);

  const boundary = /boundary=([^;]+)/i.exec(res.headers.get("Content-Type") || "");
  if (!boundary) throw new Error("feed sent no boundary");
  const marker = new TextEncoder().encode(`--${boundary[1].trim()}`);

  const reader = res.body.getReader();
  let buffer = new Uint8Array(0);
  try {
    for (;;) {
      const { value, done } = await reader.read();
      // A finished body is a dead feed, not a finished download: this response
      // is meant to outlive everything else on the page.
      if (done) throw new Error("feed ended");
      buffer = concatBytes(buffer, value);

      for (let taken = takeFrame(buffer, marker); taken;
           taken = takeFrame(buffer, marker)) {
        buffer = taken.rest;
        onFrame(taken.frame);
      }
      if (buffer.length > CAMERA_MAX_BUFFER) throw new Error("feed lost sync");
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

function attachCamera() {
  clearTimeout(cameraRetry);
  cameraRetry = null;
  if (cameraAbort) return;                       // already connected

  const controller = new AbortController();
  cameraAbort = controller;

  let watchdog = setTimeout(() => controller.abort(), CAMERA_TIMEOUT);
  const onFrame = (frame) => {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => controller.abort(), CAMERA_TIMEOUT);
    showFrame(frame);
  };

  readCamera(controller.signal, onFrame)
    .catch((err) => {
      // A deliberate detach clears cameraAbort before aborting, so this tells
      // "we hung up" apart from "it died on us".
      if (cameraAbort !== controller) return;
      cameraAbort = null;
      cameraFailed(err);
    })
    .finally(() => clearTimeout(watchdog));
}

function cameraFailed(err) {
  const reason = String(err && err.message);
  if (reason === "off") {
    // Switched off rather than broken. Refreshing the status hides the whole
    // section, which is the honest thing to show, and stops the backoff
    // complaining about a camera nobody is being denied.
    detachCamera();
    refreshStatus();
    return;
  }

  el.cameraFrame.hidden = true;
  el.cameraNote.textContent =
    reason === "busy"
      ? "Too many people are watching right now."
      : cameraFailures
      ? "Still can't reach the camera."
      : "The camera is unreachable right now.";

  // Back off rather than hammering a camera that is already not answering.
  cameraFailures += 1;
  const delay = Math.min(30000, 2000 * 2 ** (cameraFailures - 1));
  cameraRetry = setTimeout(() => {
    cameraRetry = null;
    if (!el.camera.hidden) attachCamera();
  }, delay);
}

function detachCamera() {
  clearTimeout(cameraRetry);
  cameraRetry = null;
  const controller = cameraAbort;
  cameraAbort = null;
  // Hanging up closes the connection. Without it the server keeps streaming to
  // a page nobody is looking at, and counts a viewer who has already left
  // against the cap.
  if (controller) controller.abort();
  if (el.cameraImg.src.startsWith("blob:")) URL.revokeObjectURL(el.cameraImg.src);
  el.cameraImg.removeAttribute("src");
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
  if (!cameraAbort && cameraRetry === null) attachCamera();
}

// Waking a laptop or returning to a backgrounded tab is exactly the case that
// used to need a reload: the connection died while nobody was looking. Don't
// make someone sit out a backoff that was counted while the tab was hidden.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (el.camera.hidden || cameraAbort) return;
  cameraFailures = 0;
  attachCamera();
});

/* -- status -------------------------------------------------------------- */

function setStatus(cls, text) {
  el.status.className = `status status--${cls}`;
  el.status.textContent = text;
}

// variant "info" is for neither-failure-nor-success: a queued message is not
// an error, and colouring it like one reads as a rejection.
function showError(msg, ok = false, variant = "") {
  el.error.hidden = false;
  el.error.textContent = msg;
  el.error.classList.toggle("error--ok", ok);
  el.error.classList.toggle("error--info", variant === "info");
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

/* -- proof of work ------------------------------------------------------- */

// Every print has to arrive with a solved challenge. The search takes a
// fraction of a second, and it is started as soon as there is any sign someone
// is composing a message - so by the time the button is pressed the answer is
// almost always already sitting here and the print goes out immediately.
//
// This is the check a checkbox could never be. The flood that made it
// necessary never loaded this page: it posted straight to /api/print, where no
// amount of clicking happens. What a sender cannot skip is arriving with proof
// that the work was done.

let solvedProof = null;         // an answer waiting to be spent
let solveInFlight = null;       // a search already running, if any

async function freshProof(onProgress) {
  const r = await fetch("/api/challenge", { cache: "no-store" });
  if (!r.ok) throw new Error(`challenge ${r.status}`);
  const { challenge, bits } = await r.json();
  if (!bits) return { challenge: "", counter: 0 };   // switched off server-side
  const { counter } = await Pow.solve(challenge, bits, onProgress);
  return { challenge, counter };
}

// Start early and quietly. A failure here is not worth showing: the submit
// path solves one itself if this has not produced anything.
function warmProof() {
  if (solvedProof || solveInFlight) return;
  solveInFlight = freshProof()
    .then((proof) => { solvedProof = proof; })
    .catch(() => {})
    .finally(() => { solveInFlight = null; });
}

async function takeProof(onProgress) {
  if (!solvedProof && solveInFlight) await solveInFlight;
  if (solvedProof) {
    const proof = solvedProof;
    solvedProof = null;         // single use, on this side as well as the server's
    return proof;
  }
  return freshProof(onProgress);
}

/* -- the puzzle ---------------------------------------------------------- */

// Shown only when a message has been queued because the printer is under
// attack. It is a fast lane, not a gate: ignoring it leaves the message in the
// queue exactly as before, which is why a picture is an acceptable thing to
// ask for. Nobody is locked out by failing to see it.
//
// The overlay is an even 3x2 grid rather than one hit box per drawn shape.
// Each shape sits comfortably inside its third of the image, so a click
// anywhere in the right region counts - which is kinder on a phone than
// asking for precision.
async function askPuzzle() {
  let issued;
  try {
    const r = await fetch("/api/captcha", { cache: "no-store" });
    if (!r.ok) return null;
    issued = await r.json();
  } catch {
    return null;
  }

  el.puzzleImg.src = issued.image;
  el.puzzleGrid.style.gridTemplateColumns = `repeat(${issued.columns}, 1fr)`;
  el.puzzle.hidden = false;

  return new Promise((resolve) => {
    const done = (value) => {
      el.puzzle.hidden = true;
      el.puzzleGrid.replaceChildren();
      resolve(value);
    };

    el.puzzleGrid.replaceChildren(...Array.from({ length: issued.tiles }, (_, i) => {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "puzzle__cell";
      cell.setAttribute("aria-label", `Shape ${i + 1}`);
      cell.addEventListener("click", () => done({ token: issued.token, answer: i }));
      return cell;
    }));

    el.puzzleSkip.onclick = () => done(null);
  });
}

/* -- submit -------------------------------------------------------------- */

el.form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  clearError();

  const message = asTyped(el.message.value);
  if (!message.trim()) { showError("Nothing to print."); return; }

  el.submit.disabled = true;
  el.submit.textContent = "Printing…";

  const showSearch = (tried) => {
    el.submit.textContent = `Checking… ${Math.round(tried / 1000)}k`;
  };

  const post = (proof, puzzle) => fetch("/api/print", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      name: el.name.value.trim(),
      challenge: proof.challenge,
      counter: proof.counter,
      // Only present after a queued message and a solved puzzle. The rest of
      // the time the server never looks at these.
      captcha_token: puzzle ? puzzle.token : "",
      captcha_answer: puzzle ? puzzle.answer : -1,
    }),
  });

  try {
    let r = await post(await takeProof(showSearch));

    // 428 means the proof was stale or already spent - a challenge lives for
    // five minutes, and someone can easily spend longer writing. Solving a
    // fresh one and sending again is better than making them press the button
    // twice for a reason they cannot see.
    if (r.status === 428) {
      el.submit.textContent = "Printing…";
      r = await post(await freshProof(showSearch));
    }

    const body = await r.json().catch(() => ({}));

    // 202: the printer is under attack and the message is queued rather than
    // printed. If a puzzle is on offer, solving it prints now instead.
    if (r.status === 202) {
      showError(body.detail, false, "info");
      el.submit.textContent = "Print it";
      syncSubmit();
      if (body.captcha_offered) {
        const puzzle = await askPuzzle();
        if (puzzle) {
          el.submit.disabled = true;
          el.submit.textContent = "Printing…";
          const second = await post(await takeProof(showSearch), puzzle);
          const again = await second.json().catch(() => ({}));
          if (second.ok) {
            showError("Printed. It is sitting on my desk right now.", true);
            el.message.value = "";
            updateCount();
            renderPreview();
            startCooldown(again.next_allowed_in || limits.cooldown_seconds);
          } else if (second.status === 202) {
            showError("That was not it. Your message is still in the queue.");
          } else {
            showError(again.detail || `Something went wrong (${second.status}).`);
          }
          el.submit.textContent = "Print it";
          syncSubmit();
        }
      }
    } else if (r.ok) {
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
  } finally {
    warmProof();               // ready for the next one
  }
});

el.message.addEventListener("input", () => {
  warmProof();
  updateCount();
  renderPreview();
});
el.name.addEventListener("input", renderPreview);

refreshStatus();
setInterval(refreshStatus, 60000);
