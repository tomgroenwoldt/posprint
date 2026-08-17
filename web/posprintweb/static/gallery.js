"use strict";

// Every message here was typed by a stranger. It goes into the page with
// textContent and nothing else - never innerHTML, never a template string that
// ends up assigned to one. The receipt preview on the print page escapes and
// then assigns HTML because it interleaves markup for the double-width header;
// there is no such need here, so this takes the stronger guarantee.

const $ = (id) => document.getElementById(id);
const el = { entries: $("entries"), more: $("more"), empty: $("empty") };

let cursor = null;
let columns = 48;

function stamp(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function entryNode(entry) {
  const li = document.createElement("li");
  li.className = "gallery__item";

  const paper = document.createElement("pre");
  paper.className = "gallery__paper";
  paper.textContent = entry.message;

  const meta = document.createElement("p");
  meta.className = "gallery__meta";
  // Assembled from two text nodes rather than one string so the name cannot
  // be mistaken for markup even if this is refactored later.
  meta.append(
    document.createTextNode(entry.name ? `from ${entry.name}` : "from someone on the internet"),
    document.createTextNode(` · ${stamp(entry.ts)}`),
  );

  li.append(paper, meta);
  return li;
}

async function load() {
  el.more.disabled = true;
  try {
    const url = cursor === null ? "/api/gallery" : `/api/gallery?before=${cursor}`;
    const r = await fetch(url);
    const body = await r.json();

    columns = body.columns || columns;
    document.documentElement.style.setProperty("--cols", String(columns));

    for (const entry of body.entries) el.entries.append(entryNode(entry));

    // The server sends a cursor only when the page came back full, so a short
    // page ends the list. Wrong only in the harmless direction: when the total
    // is an exact multiple of the page size, one extra request returns nothing.
    cursor = body.next;
    el.more.hidden = cursor === null;
    el.empty.hidden = el.entries.children.length > 0;
  } catch {
    el.empty.textContent = "Could not load the gallery.";
    el.empty.hidden = false;
  } finally {
    el.more.disabled = false;
  }
}

el.more.addEventListener("click", load);
load();
