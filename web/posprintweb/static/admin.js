"use strict";

// Auth by URL fragment: open /admin#<admin key>.
//
// A fragment is never sent to the server, so unlike a query string it cannot
// appear in Caddy's access log, in an upstream request line, or in a Referer
// header. It is moved straight into sessionStorage and struck from the address
// bar and the back stack, so a shoulder-surfer or a screenshot gets nothing
// either. From then on it travels as the same X-Admin-Key header the API has
// always used - no cookie, no session, no second credential format.

const $ = (id) => document.getElementById(id);
const el = { queue: $("queue"), counts: $("counts"), error: $("error"), empty: $("empty") };

const KEY_NAME = "posprintweb-admin-key";

function takeKey() {
  const fragment = location.hash.replace(/^#/, "").trim();
  if (fragment) {
    sessionStorage.setItem(KEY_NAME, fragment);
    // replaceState rather than assigning location.hash: this leaves no entry
    // in history, so Back cannot walk to a URL containing the key.
    history.replaceState(null, "", location.pathname);
  }
  return sessionStorage.getItem(KEY_NAME) || "";
}

const KEY = takeKey();

function fail(msg) {
  el.error.textContent = msg;
  el.error.hidden = false;
}

async function api(path, options = {}) {
  const r = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), "X-Admin-Key": KEY },
  });
  if (r.status === 404) throw new Error("unauthorised");
  if (!r.ok) throw new Error(`request failed (${r.status})`);
  return r.json();
}

function stamp(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Same rule as the gallery: strangers' text reaches the DOM only as text.
function itemNode(entry) {
  const li = document.createElement("li");
  li.className = "gallery__item";

  const paper = document.createElement("pre");
  paper.className = "gallery__paper";
  paper.textContent = entry.message;

  const meta = document.createElement("p");
  meta.className = "gallery__meta";
  meta.textContent =
    `${entry.name || "(no name)"} · ${entry.ip} · ${stamp(entry.ts)}`;

  const actions = document.createElement("p");
  actions.className = "review__actions";
  for (const [action, label] of [["approve", "Approve"], ["hide", "Hide"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.className = action === "approve" ? "" : "button--quiet";
    button.addEventListener("click", async () => {
      actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        const body = await api("/api/admin/gallery", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: entry.id, action }),
        });
        li.remove();
        showCounts(body.counts);
        el.empty.hidden = el.queue.children.length > 0;
      } catch (err) {
        fail(String(err.message));
        actions.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    });
    actions.append(button);
  }

  li.append(paper, meta, actions);
  return li;
}

function showCounts(counts) {
  el.counts.textContent =
    `${counts.new} waiting · ${counts.approved} approved · ${counts.hidden} hidden`;
}

async function load() {
  if (!KEY) {
    fail("Add your admin key to the URL: /admin#your-key");
    return;
  }
  try {
    const body = await api("/api/admin/queue");
    el.queue.replaceChildren(...body.queue.map(itemNode));
    showCounts(body.counts);
    el.empty.hidden = body.queue.length > 0;
  } catch (err) {
    if (err.message === "unauthorised") {
      // The API 404s rather than 401s so it does not confirm the endpoint to a
      // stranger. Here, where the key is expected, say what is actually wrong.
      sessionStorage.removeItem(KEY_NAME);
      fail("That key was not accepted. Open /admin#your-key again.");
    } else {
      fail(err.message);
    }
  }
}

load();
