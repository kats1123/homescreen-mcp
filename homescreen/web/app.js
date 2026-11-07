// Homescreen app. Every change goes through /api/op on the server, which edits the draft as
// one undoable step and returns the new view; nothing reaches the phone until Apply.
"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

let V = null;               // current view from the server
const sel = new Set();      // selected item ids
let openFolder = null;      // id of the folder shown in the drawer
let drag = null;            // { ids }
let query = "";

// ---- server ----------------------------------------------------------------------------

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Token": window.HS_TOKEN },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) {
    if (data.view) setView(data.view);
    throw new Error(data.error || `request failed (${r.status})`);
  }
  return data;
}

async function op(name, args = {}, quiet = false) {
  try {
    setView(await api("/api/op", { op: name, args }));
    return true;
  } catch (e) {
    if (!quiet) toast(e.message, "error");
    return false;
  }
}

async function withBusy(text, fn) {
  $("#busy-text").textContent = text;
  $("#busy").hidden = false;
  try { return await fn(); } finally { $("#busy").hidden = true; }
}

// ---- helpers ---------------------------------------------------------------------------

function iconImg(item, cls) {
  const wrap = el("div", cls || "tile");
  if (item.icon) {
    const img = new Image();
    img.loading = "lazy";
    img.alt = "";
    img.src = "/icon?id=" + encodeURIComponent(item.icon);
    img.onerror = () => { img.remove(); fallback(wrap, item.name); };
    wrap.appendChild(img);
  } else {
    fallback(wrap, item.name);
  }
  return wrap;
}

function fallback(wrap, name) {
  let h = 0;
  for (const c of name || "?") h = (h * 31 + c.charCodeAt(0)) % 360;
  wrap.style.background = `hsl(${h} 45% 52%)`;
  wrap.textContent = (name || "?").trim().charAt(0).toUpperCase();
}

// the device being edited: "iPad" or "iPhone", and its grid (iPads report their own)
const D = () => ((V && V.device && V.device.ProductType) || "").startsWith("iPad") ? "iPad" : "iPhone";
const G = () => (V && V.grid) || { cols: 4, rows: 6, dock: 4, folder: 9 };

const matches = (item) => !query || item.name.toLowerCase().includes(query) || item.id.toLowerCase().includes(query);

function findItem(id) {
  if (!V || !V.loaded) return null;
  const pools = [V.dock, ...V.pages, V.library || [], V.bank || []];
  for (const pool of pools) {
    for (const it of pool) {
      if (it.id === id) return it;
      for (const fp of it.pages || []) for (const c of fp) if (c.id === id) return c;
    }
  }
  return null;
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, kind === "error" ? 6000 : 3000);
}

function modal({ title, body, actions }) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    const b = $("#modal-body");
    b.replaceChildren();
    if (typeof body === "string") b.appendChild(el("p", null, body));
    else if (body) b.appendChild(body);
    const a = $("#modal-actions");
    a.replaceChildren();
    const close = (v) => { $("#modal").hidden = true; document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => {
      if (e.key === "Escape") close(null);
      if (e.key === "Enter" && !e.target.matches("textarea")) {
        const def = actions.find((x) => x.primary);
        if (def) close(def.value());
      }
    };
    for (const act of actions) {
      const btn = el("button", act.primary ? "primary" : act.danger ? "danger" : "", act.label);
      btn.onclick = () => close(act.value ? act.value() : null);
      a.appendChild(btn);
    }
    document.addEventListener("keydown", onKey);
    $("#modal").hidden = false;
    const inp = b.querySelector("input");
    (inp || a.querySelector(".primary") || a.lastChild).focus();
    if (inp) inp.select();
  });
}

async function promptText(title, label, value) {
  const wrap = el("div");
  if (label) wrap.appendChild(el("p", null, label));
  const inp = el("input");
  inp.value = value || "";
  wrap.appendChild(inp);
  return modal({ title, body: wrap, actions: [
    { label: "Cancel", value: () => null },
    { label: "OK", primary: true, value: () => inp.value.trim() || null },
  ] });
}

const confirmBox = (title, text, label = "Continue", danger = false) => modal({ title, body: text, actions: [
  { label: "Cancel", value: () => false },
  { label, primary: !danger, danger, value: () => true },
] });

// ---- rendering --------------------------------------------------------------------------

function setView(v) {
  V = v;
  for (const id of [...sel]) if (!findItem(id)) sel.delete(id);
  if (openFolder && !findItem(openFolder)) openFolder = null;
  render();
}

function render() {
  renderConnection();
  const loaded = V && V.loaded;
  $("#empty").hidden = loaded;
  $("#board").hidden = !loaded;
  $("#library-panel").hidden = !loaded;
  for (const id of ["undo", "redo", "reset", "organize-btn", "search"]) $("#" + id).disabled = !loaded;
  if (!loaded) {
    $("#device").textContent = V && V.source && V.source !== "phone" ? V.source : (deviceLabel() || "No device");
    $("#read").textContent = "Read device";
    renderFooter();
    return;
  }
  const d = V.device || {};
  $("#device").textContent = [d.DeviceName, d.ProductVersion && (D() === "iPad" ? "iPadOS " : "iOS ") + d.ProductVersion].filter(Boolean).join(" · ") || V.source;
  $("#read").textContent = "Read " + D();
  $("#undo").disabled = !V.history.undo;
  $("#redo").disabled = !V.history.redo;
  renderDock();
  renderPages();
  renderLibrary();
  renderBank();
  renderUsage();
  renderDrawer();
  renderSelection();
  renderFooter();
}

function renderItem(item, container) {
  const moved = V.moved.includes(item.id);
  if (item.kind === "widget") {
    const w = el("div", `widget w-${item.size}`);
    w.dataset.id = item.id;
    const ic = iconImg(item, "wicon");
    w.append(ic, el("div", "wname", item.name), el("div", "wkind", `${item.stack ? "Smart Stack" : "Widget"} · ${item.size}`));
    w.title = `${item.name} (${item.stack ? "Smart Stack" : "widget"}, ${item.size})`;
    wireDrag(w, item, container);
    return w;
  }
  const node = el("div", "item" + (item.kind === "folder" ? " folder" : ""));
  node.dataset.id = item.id;
  if (item.kind === "folder") {
    const tile = el("div", "tile");
    const kids = (item.pages || []).flat().slice(0, 9);
    for (const k of kids) tile.appendChild(iconImg(k, "mini"));
    node.appendChild(tile);
  } else {
    node.appendChild(iconImg(item));
  }
  node.appendChild(el("div", "name", item.name));
  node.title = item.kind === "folder"
    ? `${item.name}: ${(item.pages || []).flat().length} apps (click to open)` : `${item.name}\n${item.id}`;
  if (sel.has(item.id)) node.classList.add("selected");
  if (moved) node.classList.add("moved");
  if (query) node.classList.add(matches(item) || (item.pages || []).flat().some(matches) ? "hit" : "dim");
  node.addEventListener("click", (e) => onItemClick(e, item));
  wireDrag(node, item, container);
  return node;
}

function renderDock() {
  const dock = $("#dock");
  dock.style.setProperty("--dock-cols", Math.max(4, V.dock.length + (V.dock.length < G().dock ? 1 : 0)));
  dock.replaceChildren(...V.dock.map((it) => renderItem(it, { target: "dock" })));
}

function renderPages() {
  const box = $("#pages");
  box.replaceChildren();
  V.pages.forEach((page, i) => {
    const n = i + 1;
    const card = el("div", "page");
    const head = el("div", "page-head");
    head.appendChild(el("span", "num", `Page ${n}`));
    const used = page.reduce((a, it) => a + (it.slots || 1), 0);
    const over = V.diff.problems.some((p) => p.startsWith(`page ${n} needs`));
    const trusted = used > V.slots.page && !over;  // an iPad widget page as iPadOS laid it out
    const slots = el("span", "slots" + (over ? " over" : ""), trusted ? "as on device" : `${used}/${V.slots.page}`);
    if (trusted) slots.title = "iPad widgets don't map exactly onto the icon grid; this page keeps the shape iPadOS gave it";
    head.appendChild(slots);
    const tools = el("div", "tools");
    const tb = (label, title, fn, disabled) => {
      const b = el("button", null, label);
      b.title = title;
      b.disabled = !!disabled;
      b.onclick = fn;
      tools.appendChild(b);
    };
    tb("◀", "Move page left", () => op("move_page", { page: n, to: n - 1 }), n === 1);
    tb("▶", "Move page right", () => op("move_page", { page: n, to: n + 1 }), n === V.pages.length);
    tb("＋", "Insert a page after this one", () => op("insert_page", { page: n + 1 }));
    tb("✕", "Delete page (apps go to the App Library)", async () => {
      if (page.length && !(await confirmBox(`Delete page ${n}?`,
        "Its apps, including those in folders, move to the App Library. They stay installed.", "Delete page", true))) return;
      op("delete_page", { page: n });
    });
    head.appendChild(tools);
    card.appendChild(head);
    const grid = el("div", "grid");
    const { cols, rows } = G();
    grid.style.setProperty("--cols", cols);
    grid.style.setProperty("--rows", rows);
    const ctx = { target: `page:${n}` };
    const taken = new Set();
    for (const it of page) {
      const node = renderItem(it, ctx);
      if (it.pos) {
        const [r, c, w, h] = it.pos;
        node.style.gridRow = `${r + 1} / span ${h}`;
        node.style.gridColumn = `${c + 1} / span ${w}`;
        for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) taken.add((r + y) * cols + c + x);
      }
      if (it.pinned) node.classList.add("pinned");
      grid.appendChild(node);
    }
    for (let cell = 0; cell < cols * rows && !trusted; cell++) {
      if (taken.has(cell)) continue;
      const slot = el("div", "slot");
      slot.style.gridRow = `${Math.floor(cell / cols) + 1}`;
      slot.style.gridColumn = `${(cell % cols) + 1}`;
      slot.title = "Drop here to place an app exactly in this spot";
      wireSlot(slot, n, cell);
      grid.appendChild(slot);
    }
    card.appendChild(grid);
    wireZone(card, ctx);
    box.appendChild(card);
  });
  const add = el("div", "add-page", "＋ New page");
  add.title = "Drop apps here to put them on a new page";
  add.onclick = () => op("insert_page", { page: V.pages.length + 1 });
  wireZone(add, { target: `page:${V.pages.length + 1}`, newPage: true });
  box.appendChild(add);
}

function renderLibrary() {
  const list = $("#library");
  list.replaceChildren();
  if (V.library === null) {
    $("#lib-count").textContent = "";
    list.appendChild(el("div", "lib-empty", "This device doesn't expose the App Library."));
    return;
  }
  const items = V.library.filter(matches).sort((a, b) => a.name.localeCompare(b.name));
  $("#lib-count").textContent = query ? `${items.length} of ${V.library.length}` : String(V.library.length);
  if (!items.length) list.appendChild(el("div", "lib-empty", query ? "No matches" : "Drag apps here to keep them only in the App Library."));
  for (const it of items) {
    const row = el("div", "lib-row");
    row.dataset.id = it.id;
    row.append(iconImg(it), el("span", "n", it.name));
    row.title = it.id;
    if (sel.has(it.id)) row.classList.add("selected");
    if (V.moved.includes(it.id)) row.classList.add("moved");
    row.addEventListener("click", (e) => onItemClick(e, it));
    wireDrag(row, it, { target: "library" });
    list.appendChild(row);
  }
}

function renderBank() {
  const bank = $("#bank");
  bank.replaceChildren(...V.bank.map((it) => renderItem(it, { target: "bank" })));
  if (!V.bank.length) bank.appendChild(el("div", "empty-note", "Drag items here to set them aside."));
  $("#bank-count").textContent = V.bank.length ? String(V.bank.length) : "";
}

function renderUsage() {
  const box = $("#usage");
  box.replaceChildren(el("h2", null, "App usage"));
  const u = V.usage;
  if (!u || !u.ok) {
    box.appendChild(el("p", "note", u && u.error ? u.error
      : `See which apps get used and how many page swipes your layout costs. Read it from the ${D()}'s own memory reports, or from Screen Time synced to this Mac.`));
    const row = el("div", "usage-actions");
    const a = el("button", "primary", `From the ${D()}`);
    a.onclick = () => withBusy(`Reading usage from the ${D()}…`, () => op("load_usage", { source: "device" }));
    const b = el("button", null, "From Screen Time");
    b.title = "Needs Screen Time › Share Across Devices, and Full Disk Access for this app";
    b.onclick = () => withBusy("Reading Screen Time…", () => op("load_usage", { source: "mac" }));
    row.append(a, b);
    box.appendChild(row);
    return;
  }
  const max = Math.max(u.original, u.now, 1);
  const bars = el("div", "bars");
  for (const [label, val, cls] of [["On " + D(), u.original, ""], ["This draft", u.now, "now"]]) {
    const b = el("div", "ubar " + cls);
    const i = el("i");
    i.style.height = `${Math.max(4, Math.round((val / max) * 62))}px`;
    b.append(el("b", null, val.toLocaleString()), i, el("span", null, label));
    bars.appendChild(b);
  }
  box.appendChild(el("p", "note", u.source === "device"
    ? `From ${u.reports} memory reports on the ${D()} over ${u.days} days. Estimated page swipes:`
    : `Estimated page swipes, last ${u.days} days of Screen Time`));
  box.appendChild(bars);
  if (u.original) {
    const pct = Math.round((1 - u.now / u.original) * 100);
    box.appendChild(el("div", "gain" + (pct < 0 ? " worse" : ""),
      pct > 0 ? `${pct}% fewer swipes` : pct < 0 ? `${-pct}% more swipes` : "Same as on the " + D()));
  }
  const ul = el("ul", "top");
  for (const a of u.top) {
    const li = el("li");
    li.append(iconImg({ icon: a.id, name: a.name }), el("span", "n", a.name),
      el("span", "w", u.source === "device" ? a.where : `${a.opens.toLocaleString()} · ${a.where}`));
    ul.appendChild(li);
  }
  box.appendChild(ul);
  if (u.background && u.background.length) {
    const n = el("p", "note bg", `Counted less, since iOS also runs them in the background: ${u.background.slice(0, 8).join(", ")}${u.background.length > 8 ? "…" : ""}`);
    box.appendChild(n);
  }
}

function renderDrawer() {
  const folder = openFolder && findItem(openFolder);
  $("#drawer").hidden = !folder;
  if (!folder) return;
  const name = $("#folder-name");
  if (document.activeElement !== name) name.value = folder.name;
  const box = $("#folder-pages");
  box.replaceChildren();
  folder.pages.forEach((page, i) => {
    const fp = el("div", "fpage");
    fp.appendChild(el("div", "num", `Folder page ${i + 1} · ${page.length}/${G().folder}`));
    const g = el("div", "fgrid");
    g.style.setProperty("--fcols", Math.round(Math.sqrt(G().folder)));
    const ctx = { target: `${folder.id}:${i + 1}` };
    for (const it of page) g.appendChild(renderItem(it, ctx));
    fp.appendChild(g);
    wireZone(fp, ctx);
    box.appendChild(fp);
  });
}

function renderSelection() {
  const bar = $("#selection-bar");
  bar.hidden = sel.size === 0;
  $("#sel-count").textContent = `${sel.size} selected`;
  $("#sel-library").disabled = V.library === null;
  $("#sel-unpin").hidden = ![...sel].some((i) => (findItem(i) || {}).pinned);
}

function renderFooter() {
  const s = $("#summary");
  const btn = $("#review");
  if (!V || !V.loaded) { s.textContent = "Read your device to start"; btn.disabled = true; return; }
  const d = V.diff;
  s.replaceChildren();
  const add = (t, b) => { if (b) { s.appendChild(el("b", null, t)); } else s.appendChild(document.createTextNode(t)); };
  if (!d.items_moved && d.pages_before === d.pages_after && !d.gaps_changed) add("No changes");
  else {
    add(`${d.items_moved}`, true); add(` apps moved`);
    if (d.to_app_library) { add(" · "); add(`${d.to_app_library}`, true); add(" to App Library"); }
    if (d.pages_before !== d.pages_after) { add(" · pages "); add(`${d.pages_before} → ${d.pages_after}`, true); }
    if (d.gaps_changed) { add(" · "); add(`${d.gaps_changed}`, true); add(" gap change" + (d.gaps_changed > 1 ? "s" : "")); }
  }
  if (d.problems.length) s.appendChild(el("span", "problem", ` · ${d.problems.length} problem${d.problems.length > 1 ? "s" : ""}`));
  btn.disabled = !!(V.connection && V.connection.needs_read) ||
    (!d.items_moved && d.pages_before === d.pages_after && !d.gaps_changed);
}

// ---- interaction -----------------------------------------------------------------------

function onItemClick(e, item) {
  e.stopPropagation();
  if (e.metaKey || e.ctrlKey || e.shiftKey) {
    if (item.kind === "widget") return;
    sel.has(item.id) ? sel.delete(item.id) : sel.add(item.id);
    render();
    return;
  }
  if (item.kind === "folder") { openFolder = item.id; sel.clear(); render(); return; }
  if (sel.size && !sel.has(item.id)) sel.clear();
  sel.has(item.id) ? sel.delete(item.id) : sel.add(item.id);
  render();
}

function clearMarks() {
  document.querySelectorAll(".ins-before,.ins-after,.ins-onto,.drop-zone,.drop-cell").forEach((n) =>
    n.classList.remove("ins-before", "ins-after", "ins-onto", "drop-zone", "drop-cell"));
}

function zoneOf(e, node, item) {
  const r = node.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width;
  if (item.kind !== "widget" && x > 0.28 && x < 0.72 && !drag.ids.includes(item.id)) return "onto";
  return x < 0.5 ? "before" : "after";
}

function wireDrag(node, item, ctx) {
  node.draggable = true;
  node.addEventListener("dragstart", (e) => {
    const ids = sel.has(item.id) ? [...sel] : [item.id];
    drag = { ids };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", ids.join(","));
    requestAnimationFrame(() => node.classList.add("dragging"));
  });
  node.addEventListener("dragend", () => { node.classList.remove("dragging"); drag = null; clearMarks(); });
  if (ctx.target === "library") return; // library rows aren't insertion points
  node.addEventListener("dragover", (e) => {
    if (!drag) return;
    e.preventDefault();
    e.stopPropagation();
    clearMarks();
    node.classList.add("ins-" + zoneOf(e, node, item));
  });
  node.addEventListener("drop", (e) => {
    if (!drag) return;
    e.preventDefault();
    e.stopPropagation();
    const zone = zoneOf(e, node, item);
    const ids = drag.ids.filter((i) => i !== item.id);
    clearMarks();
    if (!ids.length) return;
    if (zone === "onto" && e.altKey && ids.length === 1) return op("swap", { a: ids[0], b: item.id });
    if (zone === "onto") return dropOnto(item, ids, ctx);
    const siblings = [...node.parentElement.children].map((c) => c.dataset.id).filter(Boolean);
    let before = item.id;
    if (zone === "after") {
      const rest = siblings.slice(siblings.indexOf(item.id) + 1).filter((i) => !ids.includes(i));
      before = rest[0] || null;
    }
    moveIds(ids, ctx.target, before);
  });
}

function wireSlot(slot, page, cell) {
  slot.addEventListener("dragover", (e) => {
    if (!drag || drag.ids.length !== 1) return;
    e.preventDefault();
    e.stopPropagation();
    clearMarks();
    slot.classList.add("drop-cell");
  });
  slot.addEventListener("dragleave", () => slot.classList.remove("drop-cell"));
  slot.addEventListener("drop", (e) => {
    if (!drag || drag.ids.length !== 1) return;
    e.preventDefault();
    e.stopPropagation();
    const id = drag.ids[0];
    clearMarks();
    op("pin", { id, page, cell }).then((ok) => ok && sel.clear());
  });
}

function wireZone(node, ctx) {
  node.addEventListener("dragover", (e) => {
    if (!drag) return;
    e.preventDefault();
    clearMarks();
    node.classList.add("drop-zone");
  });
  node.addEventListener("dragleave", (e) => { if (!node.contains(e.relatedTarget)) node.classList.remove("drop-zone"); });
  node.addEventListener("drop", (e) => {
    if (!drag) return;
    e.preventDefault();
    const ids = drag.ids;
    clearMarks();
    moveIds(ids, ctx.target, null);
  });
}

async function moveIds(ids, target, before) {
  if (target === "library") {
    const bad = ids.map(findItem).filter((it) => it && it.kind !== "app" && it.kind !== "webclip");
    if (bad.length) return toast("Only apps can go to the App Library", "error");
  }
  const ok = ids.length === 1
    ? await op("move", { id: ids[0], target, before })
    : await op("move_many", { ids, target, before });
  if (ok) sel.clear(), render();
}

async function dropOnto(item, ids, ctx) {
  if (item.kind === "folder") {
    const pages = item.pages || [[]];
    const last = pages.length;
    const room = G().folder - (pages[last - 1] || []).length;
    const target = `${item.id}:${room >= ids.length ? last : last + 1}`;
    if (await op("move_many", { ids, target })) { sel.clear(); toast(`Moved into ${item.name}`); }
    return;
  }
  const name = await promptText("New folder", `A folder with ${item.name} and ${ids.length} more.`, "Folder");
  if (!name) return;
  const page = ctx.target;
  const siblings = locateIndex(item.id, page);
  if (await op("create_folder", { name, ids: [item.id, ...ids], target: page, index: siblings })) sel.clear(), render();
}

function locateIndex(id, target) {
  const list = target === "dock" ? V.dock : target.startsWith("page:") ? V.pages[+target.slice(5) - 1] : [];
  const i = (list || []).findIndex((x) => x.id === id);
  return i < 0 ? null : i;
}

async function newFolderFromSelection() {
  const ids = [...sel].filter((i) => { const it = findItem(i); return it && it.kind !== "folder" && it.kind !== "widget"; });
  if (!ids.length) return;
  const name = await promptText("New folder", `Make a folder from ${ids.length} app${ids.length > 1 ? "s" : ""}.`, "Folder");
  if (!name) return;
  // put it where the first selected app on a page is, else at the end of the last page
  let target = `page:${V.pages.length || 1}`, index = null;
  for (let p = 0; p < V.pages.length; p++) {
    const i = V.pages[p].findIndex((x) => ids.includes(x.id));
    if (i >= 0) { target = `page:${p + 1}`; index = i; break; }
  }
  if (await op("create_folder", { name, ids, target, index })) { sel.clear(); render(); }
}

async function selectionToLibrary() {
  const ids = [...sel].filter((i) => { const it = findItem(i); return it && (it.kind === "app" || it.kind === "webclip"); });
  if (ids.length) await moveIds(ids, "library", null);
}

// ---- devices ------------------------------------------------------------------------------

let DEV = { switching: false, current: null, devices: [] };

const deviceLabel = () => {
  const d = DEV.devices.find((x) => x.udid === DEV.current);
  return d ? d.name || d.udid : "";
};

async function loadDevices() {
  try { DEV = await api("/api/devices"); } catch (e) { toast(e.message, "error"); return; }
  $("#device-btn").classList.toggle("static", !DEV.switching);
  // Only choose automatically on the first launch with exactly one device.
  // A missing/disconnected selection must never switch to another physical device.
  const connected = DEV.devices.filter((d) => d.connected);
  if (DEV.switching && !DEV.current && connected.length === 1) {
    return selectDevice(connected[0].udid, true);
  }
  renderDeviceMenu();
  if (!V || !V.loaded) render();
}

function renderDeviceMenu() {
  const m = $("#device-menu");
  m.replaceChildren();
  if (!DEV.devices.length) {
    m.appendChild(el("div", "menu-note", "No devices found. Connect over USB, or unlock a previously paired device on the same Wi-Fi network."));
  }
  for (const d of DEV.devices) {
    const b = el("button", "device-row" + (d.udid === DEV.current ? " current" : ""));
    const dot = el("i", d.connected ? "on" : "off");
    const text = el("span", "t");
    text.append(el("b", null, d.name || d.udid),
      el("small", null, [d.version && ((d.model || "").startsWith("iPad") ? "iPadOS " : "iOS ") + d.version,
        d.connected ? d.connection : "not connected", d.draft && "draft saved"].filter(Boolean).join(" · ")));
    b.append(dot, text);
    b.onclick = () => { m.hidden = true; if (d.udid !== DEV.current) selectDevice(d.udid); };
    m.appendChild(b);
  }
  const r = el("button", "menu-foot", "Refresh list");
  r.onclick = (e) => { e.stopPropagation(); loadDevices(); };
  m.appendChild(r);
}

async function selectDevice(udid, quiet) {
  if (!quiet && V && V.loaded && V.diff.items_moved) {
    toast("Your changes are kept as this device's draft", "ok");
  }
  try {
    setView(await api("/api/device", { udid }));
    DEV.current = udid;
    sel.clear();
    openFolder = null;
    renderDeviceMenu();
    render();
  } catch (e) { toast(e.message, "error"); }
}

function renderConnection() {
  const c = (V && V.connection) || {};
  const physical = ["usb", "wifi", "auto"].includes(c.mode);
  $("#transport").disabled = !physical;
  if (physical) $("#transport").value = c.mode;
  $("#pair-wifi").disabled = !physical;
  $("#reconnect").disabled = !physical;
  const label = c.transport === "wifi" ? "Wi-Fi" : "USB";
  const text = c.needs_read ? "Write unverified — reconnect, then Read device before continuing"
    : c.error ? c.error
    : !physical ? "Fixture / Simulator"
    : c.state === "ready" ? `Last check: ${label} · ready`
    : c.state === "connected" ? `Connected via ${label}`
    : "Select a device and read or reconnect";
  $("#connection-status").textContent = text;
  $("#connection-status").classList.toggle("problem", !!(c.error || c.needs_read));
}

$("#transport").onchange = async (e) => {
  try { setView(await api("/api/connection", { mode: e.target.value })); }
  catch (error) { toast(error.message, "error"); renderConnection(); }
};
$("#reconnect").onclick = async () => {
  try {
    setView(await withBusy("Reconnecting to the selected device…", () => api("/api/reconnect", {})));
    toast("Connection checked. Your draft is unchanged.", "ok");
  } catch (e) { toast(e.message, "error"); }
};
$("#pair-wifi").onclick = async () => {
  if (!(await confirmBox("Pair for Wi-Fi", "Connect the selected device with USB, unlock it and trust this Mac first. This enables wireless connections and saves pairing credentials on this Mac. Use a trusted network. Your Home Screen is unchanged.", "Pair device"))) return;
  try {
    const response = await withBusy("Pairing the selected device over USB…", () => api("/api/pair-wifi", {}));
    setView(response.view);
    toast(response.result.message, "ok");
  } catch (e) { toast(e.message, "error"); }
};

// ---- top-level actions ----------------------------------------------------------------

async function readDevice() {
  if (V && V.loaded && (V.diff.items_moved || V.history.undo)) {
    if (!(await confirmBox("Read again?", `This replaces your unapplied changes with what's on the ${D()} now.`, "Read", true))) return;
  }
  try {
    const v = await withBusy(`Reading your ${V && V.loaded ? D() : "device"}…`, () => api("/api/read", {}));
    setView(v);
    sel.clear();
    toast("Read your Home Screen", "ok");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function review() {
  const d = V.diff;
  const body = el("div");
  const stats = el("div", "stats");
  const stat = (n, t) => { const x = el("div"); x.appendChild(el("b", null, String(n))); x.appendChild(document.createTextNode(t)); stats.appendChild(x); };
  stat(d.items_moved, "apps moved");
  stat(d.to_app_library, "to App Library");
  stat(`${d.pages_before}→${d.pages_after}`, "pages");
  body.appendChild(stats);
  if (d.problems.length) {
    const ul = el("ul", "problems");
    for (const p of d.problems) ul.appendChild(el("li", null, p));
    body.appendChild(ul);
  }
  const ul = el("ul", "moves");
  for (const [k, v] of Object.entries(V.moves)) {
    const li = el("li");
    li.append(el("span", null, k.replace(/ \[.*\]$/, "")), el("span", "to", v));
    ul.appendChild(li);
  }
  body.appendChild(ul);
  if (V.gaps && !V.gaps.readable) {
    body.appendChild(el("p", null, `Gaps: iOS doesn't report empty spaces or hidden pages over the device service, so any gaps made on the ${D()} itself will close. `
      + (V.gaps.count ? `The ${V.gaps.count} placed item${V.gaps.count > 1 ? "s" : ""} you set here keep their spots.` : "To keep a gap, place the app in that spot here.")));
  }
  body.appendChild(el("p", null, `Your current layout is backed up first. After writing, the app reads the ${D()} again to check the result.`));
  const actions = [{ label: d.problems.length ? "Close" : "Cancel", value: () => false }];
  if (!d.problems.length) actions.push({ label: "Apply", primary: true, value: () => true });
  if (!(await modal({ title: d.problems.length ? "Fix these first" : `Apply to ${D()}`, body, actions }))) return;
  try {
    const res = await withBusy(`Writing to your ${D()}…`, () => api("/api/apply", { token: d.apply_token }));
    setView(res.view);
    const r = res.result;
    if (r.ok) toast(`Applied and verified on the ${D()}`, "ok");
    else showResult(r);
  } catch (e) {
    toast(e.message, "error");
  }
}

function showResult(r) {
  const clips = (r.mismatches && r.mismatches.added_by_ios || []).filter((id) => (findItem(id) || {}).kind === "webclip");
  if (r.hidden_webclips && clips.length) return showHiddenClips(r, clips);
  const body = el("div");
  body.appendChild(el("p", null, r.error || `The ${D()} doesn't match the plan exactly. Details:`));
  const pre = el("pre");
  pre.style.cssText = "white-space:pre-wrap;font-size:12px;max-height:40vh;overflow:auto";
  pre.textContent = JSON.stringify(r.mismatches || {}, null, 1);
  body.appendChild(pre);
  body.appendChild(el("p", null, `Backup saved as ${r.backup}. ` + (r.needs_read
    ? "Reconnect and read the device first. Inspect the current layout before choosing another apply or a backup restore."
    : "You can restore it from Backups.")));
  modal({ title: "Check the result", body, actions: [{ label: "OK", primary: true, value: () => null }] });
}

// iOS keeps web clips that are off the Home Screen but never reports them over USB; when a
// write frees a spot, it puts one there. Homescreen remembers them from now on.
async function showHiddenClips(r, ids) {
  const body = el("div");
  body.appendChild(el("p", null, `Everything else landed as planned. The ${D()} also had ${ids.length === 1 ? "a web clip" : `${ids.length} web clips`} `
    + "that iOS never reports, and it put them in free spots:"));
  const ul = el("ul", "moves");
  for (const n of r.hidden_webclips) ul.appendChild(el("li", null, n));
  body.appendChild(ul);
  body.appendChild(el("p", null, "Homescreen knows about them now. Send them back to the App Library, or leave them on the Home Screen and arrange them like any app."));
  const act = await modal({ title: "Hidden web clips showed up", body, actions: [
    { label: "Keep them here", value: () => "keep" },
    { label: "Send to App Library", primary: true, value: () => "library" },
  ] });
  if (act !== "library") return;
  if (!(await op("move_many", { ids, target: "library" }))) return;
  try {
    const res = await withBusy(`Writing to your ${D()}…`, () => api("/api/apply", { token: V.diff.apply_token }));
    setView(res.view);
    res.result.ok ? toast("Web clips are in the App Library", "ok") : showResult(res.result);
  } catch (e) { toast(e.message, "error"); }
}

async function showBackups() {
  const { backups } = await api("/api/backups");
  const body = el("div");
  if (!backups.length) body.appendChild(el("p", null, "No backups yet. One is saved before every apply and restore."));
  for (const b of backups) {
    const row = el("div", "backup-row");
    const m = b.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-(.*)\.plist$/);
    row.appendChild(el("span", null, m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} · ${m[7].replace(/-/g, " ")}` : b));
    const btn = el("button", null, "Restore");
    btn.onclick = async () => {
      $("#modal").hidden = true;
      if (!(await confirmBox("Restore this backup?", `This writes that layout to your ${D()}. Your current layout is backed up first.`, "Restore"))) return;
      try {
        const res = await withBusy("Restoring…", () => api("/api/restore", { name: b }));
        setView(res.view);
        res.result.ok ? toast("Restored and verified", "ok") : showResult(res.result);
      } catch (e) { toast(e.message, "error"); }
    };
    row.appendChild(btn);
    body.appendChild(row);
  }
  modal({ title: "Backups", body, actions: [{ label: "Close", primary: true, value: () => null }] });
}

async function organize(name) {
  $("#organize-menu").hidden = true;
  const keep = $("#keep-first").checked;
  if (name === "group_categories") {
    const ok = await confirmBox("Group by App Store category",
      "This looks up your apps' categories on Apple's public App Store API (only bundle IDs are sent; results are cached on this Mac). Loose apps then go into folders named after their category, joining existing folders with the same name.",
      "Look up and group");
    if (!ok) return;
    return withBusy("Looking up categories…", () => op(name, { keep_first_page: keep }));
  }
  const label = { sort_color: "Sorting by color…", optimize_usage: "Optimizing by usage…" }[name];
  const args = ["sort_color", "sort_alpha"].includes(name) ? { keep_first_page: keep }
    : name === "optimize_usage" ? { keep_first_page: false } : {};
  return label ? withBusy(label, () => op(name, args)) : op(name, args);
}

// ---- wiring ------------------------------------------------------------------------------

// the dock and library containers live for the whole session: wire them once
wireZone($("#dock"), { target: "dock" });
wireZone($("#library"), { target: "library" });
wireZone($("#bank"), { target: "bank" });
$("#read").onclick = readDevice;
$("#device-btn").onclick = (e) => {
  if (!DEV.switching) return;
  e.stopPropagation();
  const m = $("#device-menu");
  m.hidden = !m.hidden;
  if (!m.hidden) loadDevices();
};
$("#help").onclick = () => {
  const tips = [
    ["Drag an icon", "drop on the left or right edge of another icon to place it there"],
    ["Drop on an app's middle", "make a folder with both"],
    ["Drop on a folder", "put it inside"],
    ["Hold ⌥ Option while dropping on an icon", "swap the two"],
    ["Drop on an empty spot", "place it exactly there, leaving gaps (blue dot = placed)"],
    ["⌘-click", "select several, then New folder or To App Library"],
    ["Delete", "send the selected apps to the App Library"],
    ["App Bank", "park items while you rearrange; empty it before applying"],
    ["⌘Z / ⇧⌘Z", "undo / redo, as far back as you like"],
    ["⌘F", "search; matches are outlined, the rest dim"],
    ["App usage", "from the device's memory reports or Screen Time, and Organize › Optimize"],
    ["Review & Apply", "backs up the device's layout, writes yours, then reads it back to check"],
  ];
  const body = el("div");
  const ul = el("ul", "moves");
  for (const [k, v] of tips) { const li = el("li"); li.append(el("b", null, k), el("span", "to", v)); ul.appendChild(li); }
  body.appendChild(ul);
  body.appendChild(el("p", null, "Nothing changes on the device until you apply. Apps in the App Library stay there."));
  modal({ title: "How Homescreen works", body, actions: [{ label: "Got it", primary: true, value: () => null }] });
};
$("#read-empty").onclick = readDevice;
$("#undo").onclick = () => op("undo");
$("#redo").onclick = () => op("redo");
$("#reset").onclick = async () => {
  if (await confirmBox("Reset all changes?", `Go back to the layout as read from the ${D()}. You can undo this.`, "Reset", true)) op("reset");
};
$("#review").onclick = review;
$("#backups-btn").onclick = () => showBackups().catch((e) => toast(e.message, "error"));
$("#organize-btn").onclick = (e) => { e.stopPropagation(); $("#organize-menu").hidden = !$("#organize-menu").hidden; };
document.querySelectorAll("#organize-menu [data-op]").forEach((b) => { b.onclick = () => organize(b.dataset.op); });
document.addEventListener("click", (e) => {
  if (!$("#organize-menu").contains(e.target)) $("#organize-menu").hidden = true;
  if (!$("#device-menu").contains(e.target)) $("#device-menu").hidden = true;
});
$("#keep-first").checked = localStorage.getItem("hs.keepFirst") !== "0";
$("#keep-first").onchange = (e) => { try { localStorage.setItem("hs.keepFirst", e.target.checked ? "1" : "0"); } catch (_) {} };
$("#search").oninput = (e) => { query = e.target.value.trim().toLowerCase(); render(); };
$("#sel-folder").onclick = newFolderFromSelection;
$("#sel-library").onclick = selectionToLibrary;
$("#sel-unpin").onclick = () => op("unpin", { ids: [...sel] }).then((ok) => ok && (sel.clear(), render()));
$("#sel-clear").onclick = () => { sel.clear(); render(); };
$("#drawer-close").onclick = () => { openFolder = null; render(); };
$("#folder-name").onchange = (e) => {
  const name = e.target.value.trim();
  if (openFolder && name) op("rename_folder", { id: openFolder, name });
};
$("#folder-name").onkeydown = (e) => { if (e.key === "Enter") e.target.blur(); };
$("#folder-add-page").onclick = () => {
  const f = findItem(openFolder);
  if (!f) return;
  // an empty folder page appears once something is dropped on it; show a target now
  f.pages.push([]);
  renderDrawer();
};
$("#folder-dissolve").onclick = async () => {
  const f = findItem(openFolder);
  if (f && await confirmBox(`Remove “${f.name}”?`, "Its apps go back to where the folder was.", "Remove folder", true)) {
    openFolder = null;
    op("dissolve_folder", { id: f.id });
  }
};
$("#board").addEventListener("click", (e) => {
  if (!e.target.closest(".item, .widget, button") && sel.size) { sel.clear(); render(); }
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input") || !$("#modal").hidden) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); op(e.shiftKey ? "redo" : "undo"); }
  else if (e.key === "Escape") { sel.clear(); openFolder = null; render(); }
  else if ((e.key === "Backspace" || e.key === "Delete") && sel.size) { e.preventDefault(); selectionToLibrary(); }
  else if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); $("#search").focus(); }
});
window.addEventListener("beforeunload", (e) => {
  if (V && V.loaded && V.diff.items_moved) { e.preventDefault(); e.returnValue = ""; }
});

api("/api/state").then(setView).catch((e) => { toast(e.message, "error"); setView({ loaded: false }); }).then(loadDevices);
