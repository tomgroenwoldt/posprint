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
const el = {
  queue: $("queue"), counts: $("counts"), error: $("error"), empty: $("empty"),
};

const KEY_NAME = "posprintweb-admin-key";

// Which buttons each list gets, and what they do. Every decision is
// reversible, which is why "hidden" is a state rather than a delete: taking
// something down should not also destroy the record of what was sent.
const ACTIONS = {
  new: [["approve", "Approve", ""], ["hide", "Hide", "button--quiet"]],
  approved: [["hide", "Remove from gallery", "button--quiet"]],
  hidden: [["approve", "Publish", ""], ["reset", "Back to queue", "button--quiet"]],
};
const EMPTY = {
  new: "Nothing waiting. All caught up.",
  approved: "Nothing published yet.",
  hidden: "Nothing hidden.",
};

let list = "new";
let columns = 48;
let charset = null;

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

function itemNode(entry) {
  const li = document.createElement("li");
  li.className = "gallery__item";

  // The same renderer the print preview and the gallery use, so a decision is
  // made looking at what the visitor saw and what the paper showed.
  const paper = document.createElement("div");
  paper.className = "paper";
  const pre = document.createElement("pre");
  pre.className = "gallery__paper";
  pre.innerHTML = Receipt.render({
    message: entry.message,
    name: entry.name,
    when: new Date(entry.ts * 1000),
    cols: columns,
    charset,
  });
  const tear = document.createElement("div");
  tear.className = "paper__tear";
  tear.setAttribute("aria-hidden", "true");
  paper.append(pre, tear);

  // Only the admin sees the address, and only as text.
  const meta = document.createElement("p");
  meta.className = "gallery__meta";
  meta.textContent = `${entry.ip} · ${Receipt.stamp(new Date(entry.ts * 1000))}`;

  const actions = document.createElement("p");
  actions.className = "review__actions";
  for (const [action, label, cls] of ACTIONS[list]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (cls) button.className = cls;
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
        showEmpty();
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

function showEmpty() {
  el.empty.textContent = EMPTY[list];
  el.empty.hidden = el.queue.children.length > 0;
}

async function load() {
  if (!KEY) {
    fail("Add your admin key to the URL: /admin#your-key");
    return;
  }
  try {
    const body = await api(`/api/admin/queue?gallery=${list}`);
    columns = body.columns || columns;
    if (body.charset) {
      charset = {
        printable: new Set(body.charset.printable),
        replacements: body.charset.replacements,
      };
    }
    el.queue.replaceChildren(...body.queue.map(itemNode));
    showCounts(body.counts);
    showEmpty();
    el.error.hidden = true;
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

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("tab--current"));
    tab.classList.add("tab--current");
    list = tab.dataset.list;
    el.queue.replaceChildren();
    load();
  });
}

load();
