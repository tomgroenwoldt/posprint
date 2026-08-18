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
const el = {
  entries: $("entries"), more: $("more"), empty: $("empty"),
  filter: $("filter"), day: $("day"), shown: $("shown"),
};

let cursor = null;
let columns = 48;
let charset = null;
let day = new URLSearchParams(location.search).get("day");
let total = null;                  // how many exist under the current filter

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

// Built from parts rather than from new Date("2026-08-18"), which is parsed as
// UTC midnight and so reads as the day before anywhere west of Greenwich. The
// value stays the ISO day the row was written with; only the label is prose.
function dayLabel(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", {
    day: "numeric", month: "long", year: "numeric",
  });
}

// The filter is built from the days that actually have something on them, so
// every option leads somewhere and the control cannot offer an empty result.
function fillDays(days) {
  const all = days.reduce((n, d) => n + d.count, 0);
  const options = [["", `All days · ${all}`]];
  for (const d of days) options.push([d.day, `${dayLabel(d.day)} · ${d.count}`]);

  // A hand-typed ?day= for a day with nothing approved on it would otherwise
  // leave the control reading "All days" over an empty list, which looks like
  // the page is broken rather than like the day is empty.
  const known = days.find((d) => d.day === day);
  if (day && !known) options.push([day, dayLabel(day)]);

  el.day.replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }));
  el.day.value = day || "";
  el.filter.hidden = days.length === 0;
  total = day ? (known ? known.count : 0) : all;
}

// Always "x of y", including "1 of 1": a form that changes with the number
// reads better in one case and worse in the rest, and this one never has to
// worry about a plural.
function showProgress() {
  const have = el.entries.children.length;
  el.shown.textContent =
    total === null || have === 0 ? "" : `${have} of ${total}`;
}

// `first` distinguishes opening a list from extending it. Without it a second
// entry into load() with no cursor would re-fetch page one and append it to
// itself, which is only a hidden button away.
async function load(first = false) {
  if (!first && cursor === null) return;
  el.more.disabled = true;
  try {
    const q = new URLSearchParams();
    if (day) q.set("day", day);
    if (cursor !== null) q.set("before", cursor);
    const query = q.toString();
    const body = await (await fetch(`/api/gallery${query ? `?${query}` : ""}`)).json();

    columns = body.columns || columns;
    if (body.charset) {
      charset = {
        printable: new Set(body.charset.printable),
        replacements: body.charset.replacements,
      };
    }
    // Only the first request of a walk carries the day list: paging cannot
    // change it, and rebuilding the control mid-walk would reset the control
    // the visitor is looking at.
    if (body.days) fillDays(body.days);

    for (const entry of body.entries) el.entries.append(entryNode(entry));

    // The server sends a cursor only when the page came back full, so a short
    // page ends the list. Wrong only in the harmless direction: when the total
    // is an exact multiple of the page size, one extra request returns nothing.
    cursor = body.next;
    el.more.hidden = cursor === null;
    el.empty.textContent = day
      ? "Nothing from that day."
      : "Nothing here yet.";
    el.empty.hidden = el.entries.children.length > 0;
    showProgress();
  } catch {
    el.empty.textContent = "Could not load the gallery.";
    el.empty.hidden = false;
  } finally {
    el.more.disabled = false;
  }
}

// The filter lives in the URL so a day can be linked to and the back button
// walks between days rather than leaving the page.
function show(next, push) {
  day = next;
  if (push) {
    const url = day ? `/gallery?day=${encodeURIComponent(day)}` : "/gallery";
    history.pushState({ day }, "", url);
  }
  cursor = null;
  total = null;
  el.entries.replaceChildren();
  el.more.hidden = true;
  el.shown.textContent = "";
  load(true);
}

el.day.addEventListener("change", () => show(el.day.value || null, true));
window.addEventListener("popstate", () =>
  show(new URLSearchParams(location.search).get("day"), false));
el.more.addEventListener("click", () => load());
load(true);
