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
  const body = el.message.value.trim() || "…";
  const who = el.name.value.trim() || "someone on the internet";

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
}

function clearError() { el.error.hidden = true; }

function startCooldown(seconds) {
  clearInterval(cooldownTimer);
  cooldownUntil = Date.now() + seconds * 1000;
  let left = seconds;
  const tick = () => {
    if (left <= 0) {
      clearInterval(cooldownTimer);
      cooldownUntil = 0;
      el.submit.disabled = false;
      el.submit.textContent = "Print it";
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
    el.title.textContent = s.title;
    document.title = s.title;
    el.blurb.textContent = s.blurb;
    el.message.maxLength = limits.max_chars;
    el.name.maxLength = limits.max_name_chars;
    el.max.textContent = limits.max_chars;

    if (s.disabled) {
      setStatus("offline", "Printing is switched off right now.");
      el.submit.disabled = true;
    } else if (s.quiet) {
      setStatus("asleep",
        `Asleep until ${String(s.quiet_hours.end).padStart(2, "0")}:00 — ` +
        `it is ${s.local_time} there.`);
      el.submit.disabled = true;
    } else if (!s.online) {
      setStatus("offline", "The printer is offline or out of paper.");
      el.submit.disabled = true;
    } else {
      setStatus("online", "Printer is online.");
      if (cooldownUntil <= Date.now()) el.submit.disabled = false;
    }

    el.quota.textContent = s.you.remaining_today > 0
      ? `${s.you.remaining_today} of ${limits.per_ip_daily} prints left today.`
      : "No prints left today.";
    el.counterLine.textContent = `${s.printed_today} messages printed today.`;

    updateCount();
    renderPreview();
  } catch {
    setStatus("offline", "Can't reach the site's backend.");
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
      else { el.submit.disabled = false; el.submit.textContent = "Print it"; }
    }
  } catch {
    showError("Network error. Is the site still up?");
    el.submit.disabled = false;
    el.submit.textContent = "Print it";
  }
});

el.message.addEventListener("input", () => { updateCount(); renderPreview(); });
el.name.addEventListener("input", renderPreview);

refreshStatus();
setInterval(refreshStatus, 60000);
