"use strict";

// Entries are drawn by Receipt.render, the same function the print page uses
// for its preview, so what is shown here is what came off the roll - same
// 48-column wrap, same code-page degradation, same header and from-line.
//
// That function returns HTML because the double-width heading needs a span,
// and it escapes everything visitor-supplied on the way in. That escaping is
// the only thing making this safe to assign to innerHTML; nothing else on this
// page interpolates untrusted text into markup.

const $ = (id) => document.getElementById(id);
const el = { entries: $("entries"), more: $("more"), empty: $("empty") };

let cursor = null;
let columns = 48;
let charset = null;

function entryNode(entry) {
  const li = document.createElement("li");
  li.className = "gallery__item";

  const paper = document.createElement("div");
  paper.className = "paper";
  const pre = document.createElement("pre");
  pre.innerHTML = Receipt.render({
    message: entry.message,
    name: entry.name,
    when: new Date(entry.ts * 1000),
    cols: columns,
    charset,
  });
  paper.append(pre);
  li.append(paper);
  return li;
}

async function load() {
  el.more.disabled = true;
  try {
    const url = cursor === null ? "/api/gallery" : `/api/gallery?before=${cursor}`;
    const body = await (await fetch(url)).json();

    columns = body.columns || columns;
    if (body.charset) {
      charset = {
        printable: new Set(body.charset.printable),
        replacements: body.charset.replacements,
      };
    }

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
