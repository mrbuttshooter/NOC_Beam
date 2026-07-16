"use strict";
// NOC_Beam web softphone -- client logic.
//
// Security (brief rule): NEVER assign dynamic data via innerHTML. Every row /
// menu item / call card is built with createElement + textContent so a
// hostile peer URI or account label can't inject markup.

// ---- tiny DOM helper (from the prototype) ---------------------------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const $ = (id) => document.getElementById(id);

// ---- bridge handle (populated after the QWebChannel handshake) ------------
let bridge = null;

// ---- app state (last pushed snapshot) -------------------------------------
const state = {
  calls: [],
  accounts: { accounts: [], activeId: "", activeLabel: "No account", activeHealth: "muted" },
  suppliers: { visible: false, activeId: "", suppliers: [] },
  recents: [],
  history: [],
  contacts: [],
};

// ==========================================================================
// Keypad (main dialpad -- hidden while 2+ calls are active; per-call DTMF
// then lives on each card's compact pad)
// ==========================================================================
const KEYS = [["1", ""], ["2", "ABC"], ["3", "DEF"], ["4", "GHI"], ["5", "JKL"],
  ["6", "MNO"], ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"], ["*", ""], ["0", "+"], ["#", ""]];

function buildPad() {
  const pad = $("pad");
  const num = $("num");
  for (const [d, c] of KEYS) {
    const k = el("div", "key");
    k.appendChild(el("div", "d", d));
    k.appendChild(el("div", "c", c));
    k.addEventListener("click", () => {
      // Mirror ui/phone_shell.py:_on_digit_pressed -- with exactly one live
      // call, digits are DTMF to it; idle digits build the dial string.
      // (With 2+ calls this pad is hidden; per-card pads take over.)
      if (state.calls.length === 1) {
        if (bridge) bridge.send_dtmf(state.calls[0].id, d);
      } else {
        num.value += d;
        num.focus();
      }
    });
    pad.appendChild(k);
  }
}

// ==========================================================================
// Dialing
// ==========================================================================
function placeCall() {
  const num = $("num");
  const t = (num.value || "").trim();
  if (!t || !bridge) return;
  bridge.place_call(t);
  num.value = "";
}

// ==========================================================================
// Live call stack (one card per active call)
// ==========================================================================
const openPads = new Set(); // call ids whose compact DTMF pad is expanded

function fmtElapsed(anchorMs) {
  if (!anchorMs) return "";
  let s = Math.max(0, Math.floor((Date.now() - anchorMs) / 1000));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad2 = (n) => String(n).padStart(2, "0");
  return h ? `${pad2(h)}:${pad2(m)}:${pad2(s)}` : `${pad2(m)}:${pad2(s)}`;
}

// One global ticker updates every card's timer (no per-card intervals).
setInterval(() => {
  for (const t of document.querySelectorAll(".chip .t[data-anchor]")) {
    t.textContent = fmtElapsed(Number(t.dataset.anchor));
  }
}, 1000);

function ctlBtn(cls, title, glyph, handler) {
  const b = el("button", cls, glyph);
  b.title = title;
  b.addEventListener("click", (e) => { e.stopPropagation(); handler(); });
  return b;
}

function buildMiniPad(callId) {
  const padWrap = el("div", "minipad");
  for (const d of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]) {
    const k = el("div", "mkey", d);
    k.addEventListener("click", (e) => {
      e.stopPropagation();
      if (bridge) bridge.send_dtmf(callId, d);
    });
    padWrap.appendChild(k);
  }
  return padWrap;
}

function buildCard(c, multi) {
  const card = el("div", "live" + (multi ? " compact" : "") + (multi && c.selected ? " sel" : ""));

  const top = el("div", "top");
  top.appendChild(el("div", "num", c.peer || ""));
  const chip = el("div", "chip " + (c.level || "prog"));
  chip.appendChild(document.createTextNode(c.chip || ""));
  if (c.showTimer && c.anchorMs) {
    const t = el("span", "t", fmtElapsed(c.anchorMs));
    t.dataset.anchor = String(c.anchorMs);
    chip.appendChild(t);
  }
  top.appendChild(chip);
  card.appendChild(top);

  if (c.via) card.appendChild(el("div", "via", "via " + c.via));

  if (c.incoming) {
    const row = el("div", "controls incoming");
    row.appendChild(ctlBtn("btn-reject", "Reject", "Reject", () => bridge && bridge.reject(c.id)));
    row.appendChild(ctlBtn("btn-answer", "Answer", "Answer", () => bridge && bridge.answer(c.id)));
    card.appendChild(row);
  } else {
    const row = el("div", "controls");
    const mute = ctlBtn("ctl" + (c.muted ? " on" : ""), c.muted ? "Unmute" : "Mute", "🎙",
      () => bridge && bridge.toggle_mute(c.id));
    const hold = ctlBtn("ctl" + (c.held ? " on" : ""), c.held ? "Resume" : "Hold", "⏸",
      () => bridge && bridge.toggle_hold(c.id));
    const xfer = ctlBtn("ctl", "Transfer", "⇄", () => {
      const t = window.prompt("Transfer to (number or SIP URI):");
      if (t && t.trim() && bridge) bridge.transfer(c.id, t.trim());
    });
    for (const b of [mute, hold, xfer]) {
      b.disabled = !c.canControl;
      if (!c.canControl) b.style.opacity = ".4";
    }
    // Compact DTMF-pad toggle -- per-card so tones unambiguously target
    // THIS call (owner design decision, phase 2).
    const dtmf = ctlBtn("ctl" + (openPads.has(c.id) ? " on" : ""), "DTMF keypad", "⌗", () => {
      if (openPads.has(c.id)) openPads.delete(c.id); else openPads.add(c.id);
      renderCalls(state.calls);
    });
    row.appendChild(mute);
    row.appendChild(hold);
    row.appendChild(xfer);
    row.appendChild(dtmf);
    row.appendChild(ctlBtn("end", "End call", "End call", () => bridge && bridge.hangup(c.id)));
    card.appendChild(row);
    if (openPads.has(c.id)) card.appendChild(buildMiniPad(c.id));
  }

  // Click anywhere on a stacked card (not a button) promotes it to the
  // selected call (audio focus) -- Qt calls_strip parity.
  if (multi && !c.selected) {
    card.addEventListener("click", (e) => {
      if (e.target.closest("button, .mkey")) return;
      if (bridge) bridge.select_call(c.id);
    });
    card.style.cursor = "pointer";
  }
  return card;
}

let lastCallIds = "";

function renderCalls(calls) {
  const box = $("calls");
  box.textContent = "";
  const live = new Set(calls.map((c) => c.id));
  for (const id of [...openPads]) if (!live.has(id)) openPads.delete(id);
  const multi = calls.length > 1;
  // Owner design decision: 2+ active calls hide the MAIN dialpad entirely
  // to make room for the stack; each card carries its own compact pad.
  $("pad").hidden = multi;
  for (const c of calls) box.appendChild(buildCard(c, multi));
  // A NEW call (incoming ring or fresh dial) surfaces the Dial view so the
  // card is never hidden behind the History/Contacts tabs.
  const ids = calls.map((c) => c.id).join(",");
  if (calls.length && ids !== lastCallIds && activeView !== "dial") switchView("dial");
  lastCallIds = ids;
}

// ==========================================================================
// Account pill + switcher
// ==========================================================================
function renderAccounts(a) {
  $("account-label").textContent = a.activeLabel || "No account";
  const dot = $("account-dot");
  dot.className = "dot " + (a.activeHealth || "muted");
}

function openAccountMenu() {
  const menu = $("account-menu");
  menu.textContent = "";
  const a = state.accounts;
  if (!a.accounts.length) {
    menu.appendChild(el("div", "item", "No accounts"));
  } else {
    for (const acc of a.accounts) {
      const item = el("div", "item" + (acc.id === a.activeId ? " active" : ""));
      item.appendChild(el("span", "dot " + (acc.health || "muted")));
      item.appendChild(el("span", "lbl", acc.label));
      item.addEventListener("click", () => {
        if (bridge) bridge.select_account(acc.id);
        closeMenus();
      });
      menu.appendChild(item);
    }
  }
  menu.appendChild(el("div", "sep"));
  const settings = el("div", "item");
  settings.appendChild(el("span", "lbl", "Account settings…"));
  settings.addEventListener("click", () => { if (bridge) bridge.open_window("accounts"); closeMenus(); });
  menu.appendChild(settings);
  positionMenu(menu, $("account-pill"));
}

// ==========================================================================
// Supplier selector
// ==========================================================================
function renderSuppliers(s) {
  const row = $("supplier-row");
  row.hidden = !s.visible;
  if (!s.visible) return;
  const active = s.suppliers.find((x) => x.id === s.activeId);
  $("supplier-label").textContent = active ? active.label : (s.suppliers[0] ? s.suppliers[0].label : "—");
}

function openSupplierMenu() {
  const menu = $("supplier-menu");
  menu.textContent = "";
  const s = state.suppliers;
  if (!s.suppliers.length) {
    menu.appendChild(el("div", "item", "No suppliers"));
  } else {
    for (const sup of s.suppliers) {
      const item = el("div", "item" + (sup.id === s.activeId ? " active" : ""));
      item.appendChild(el("span", "lbl", sup.label));
      item.addEventListener("click", () => {
        if (bridge) bridge.select_supplier(sup.id);
        closeMenus();
      });
      menu.appendChild(item);
    }
  }
  positionMenu(menu, $("supplier-sel"), true);
}

// ==========================================================================
// Hamburger app menu
// ==========================================================================
// History/Contacts/Favorites live as in-app tabs now (phase 2); the menu
// keeps the full Qt windows as escape hatches (bulk ops / add-edit dialogs).
// BUILD_TAG renders as a muted footer in the menu — bump it on every shipped
// zip so "which build am I on?" is answerable in two clicks.
const BUILD_TAG = "build 2026-07-16.2";
const APP_MENU = [
  ["Settings", "settings"],
  ["Accounts", "accounts"],
  ["SIP trace", "trace"],
  ["Test runner", "test-runner"],
  ["sep", null],
  ["Full history", "history"],
  ["Manage contacts", "contacts"],
  ["Manage favorites", "favorites"],
  ["sep", null],
  ["Quit", "quit"],
];

function openAppMenu() {
  const menu = $("app-menu");
  menu.textContent = "";
  for (const [label, target] of APP_MENU) {
    if (label === "sep") { menu.appendChild(el("div", "sep")); continue; }
    const item = el("div", "item");
    item.appendChild(el("span", "lbl", label));
    item.addEventListener("click", () => { if (bridge) bridge.open_window(target); closeMenus(); });
    menu.appendChild(item);
  }
  menu.appendChild(el("div", "sep"));
  menu.appendChild(el("div", "build-tag", BUILD_TAG));
  positionMenu(menu, $("btn-menu"), true);
}

// ==========================================================================
// Menu positioning + dismissal (one open at a time)
// ==========================================================================
const MENU_IDS = ["account-menu", "app-menu", "supplier-menu"];

function closeMenus() {
  for (const id of MENU_IDS) $(id).hidden = true;
}
function closeMenusExcept(keep) {
  for (const id of MENU_IDS) {
    const m = $(id);
    if (m !== keep) m.hidden = true;
  }
}

// Phase-2 fix: long menus (supplier catalogs) must scroll instead of
// clipping below the window edge. Strategy: cap max-height to the free
// space below the anchor; if the list wants more room than exists below
// AND there is more space above, flip the menu upward. overflow-y on
// .menu makes the wheel work; keyboard nav below.
function positionMenu(menu, anchor, alignRight) {
  closeMenusExcept(menu);
  menu.hidden = false;
  menu.style.maxHeight = "";
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  let left = alignRight ? (r.right - mw) : r.left;
  left = Math.max(6, Math.min(left, window.innerWidth - mw - 6));

  const below = window.innerHeight - r.bottom - 10;
  const above = r.top - 10;
  const want = menu.scrollHeight + 2;
  let top;
  if (want <= below || below >= above) {
    menu.style.maxHeight = Math.max(90, below) + "px";
    top = r.bottom + 4;
  } else {
    menu.style.maxHeight = Math.max(90, above) + "px";
    top = Math.max(6, r.top - 4 - Math.min(want, above));
  }
  menu.style.left = left + "px";
  menu.style.top = top + "px";
  menu.focus();
}

// Keyboard navigation: ArrowUp/Down move the highlight (scrolling it into
// view), Enter activates, Escape closes. Attached once per menu element.
function wireMenuKeys(menu) {
  menu.tabIndex = -1;
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll(".item")];
    if (!items.length) return;
    let idx = items.findIndex((i) => i.classList.contains("kbd"));
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      idx = e.key === "ArrowDown"
        ? Math.min(items.length - 1, idx + 1)
        : Math.max(0, idx < 0 ? items.length - 1 : idx - 1);
      items.forEach((i) => i.classList.remove("kbd"));
      items[idx].classList.add("kbd");
      items[idx].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && idx >= 0) {
      e.preventDefault();
      e.stopPropagation();
      items[idx].click();
    } else if (e.key === "Escape") {
      closeMenus();
    }
  });
}

// ==========================================================================
// Recents
// ==========================================================================
function renderRecents(rows) {
  const box = $("recents");
  box.textContent = "";
  if (!rows.length) {
    box.appendChild(el("div", "empty", "No recent calls yet."));
    return;
  }
  for (const r of rows) {
    const row = el("div", "row");
    const arrowGlyph = r.dir === "in" ? "↙" : "↗";
    row.appendChild(el("span", "arrow " + r.level, arrowGlyph));
    row.appendChild(el("span", "n", r.num));
    row.appendChild(el("span", "badge " + r.level, r.status));
    row.appendChild(el("span", "w", r.time));
    const redial = el("button", "redial", "📞");
    row.appendChild(redial);
    const dial = () => { if (bridge) bridge.redial(r.uri); };
    redial.addEventListener("click", (e) => { e.stopPropagation(); dial(); });
    row.addEventListener("click", dial);
    box.appendChild(row);
  }
}

// ==========================================================================
// Tabbed views (phase 2): Dial / History / Contacts / Favorites
// ==========================================================================
const VIEWS = ["dial", "history", "contacts", "favorites"];
let activeView = "dial";

function switchView(name) {
  if (!VIEWS.includes(name)) return;
  activeView = name;
  closeMenus();
  for (const v of VIEWS) $("view-" + v).hidden = v !== name;
  for (const t of document.querySelectorAll("#tabbar .tab")) {
    t.classList.toggle("active", t.dataset.view === name);
  }
  // Fresh data on open (cheap; server caps history at 200 rows).
  if (bridge) {
    if (name === "history") bridge.refresh_history();
    if (name === "contacts" || name === "favorites") bridge.refresh_contacts();
  }
  if (name === "history") renderHistory();
  if (name === "contacts") renderContacts();
  if (name === "favorites") renderFavorites();
}

// ---- History: searchable log with expandable per-row detail --------------
let expandedHistoryKey = null;

const DETAIL_LABELS = [
  ["when", "When"], ["dialed", "Dialed"], ["peer", "Peer"],
  ["account", "Account"], ["supplier", "Supplier"], ["codec", "Codec"],
  ["result", "Result"], ["duration", "Duration"],
];

function buildDetail(d) {
  const grid = el("div", "hdetail");
  for (const [key, label] of DETAIL_LABELS) {
    const v = (d && d[key]) || "";
    if (!v) continue;
    grid.appendChild(el("span", "k", label));
    grid.appendChild(el("span", "v", v));
  }
  return grid;
}

function historyKey(r) { return (r.detail && r.detail.when) + "|" + r.uri; }

function renderHistory() {
  const box = $("history-list");
  box.textContent = "";
  const q = ($("history-search").value || "").trim().toLowerCase();
  const rows = state.history.filter((r) =>
    !q || r.num.toLowerCase().includes(q) || r.status.toLowerCase().includes(q));
  if (!rows.length) {
    box.appendChild(el("div", "empty", q ? "No matches." : "No calls yet."));
    return;
  }
  for (const r of rows) {
    const key = historyKey(r);
    const row = el("div", "row");
    row.appendChild(el("span", "arrow " + r.level, r.dir === "in" ? "↙" : "↗"));
    row.appendChild(el("span", "n", r.num));
    row.appendChild(el("span", "badge " + r.level, r.status));
    row.appendChild(el("span", "w", r.time));
    const redial = el("button", "redial", "📞");
    redial.addEventListener("click", (e) => {
      e.stopPropagation();
      if (bridge) bridge.redial(r.uri);
      switchView("dial");
    });
    row.appendChild(redial);
    // Row click toggles the info detail (one open at a time).
    row.addEventListener("click", () => {
      expandedHistoryKey = expandedHistoryKey === key ? null : key;
      renderHistory();
    });
    box.appendChild(row);
    if (expandedHistoryKey === key) box.appendChild(buildDetail(r.detail));
  }
}

// ---- Contacts / Favorites: list + search + call ---------------------------
function buildContactRow(c) {
  const row = el("div", "row");
  const name = el("div", "cname");
  name.appendChild(el("div", "nm", c.name || c.number));
  name.appendChild(el("div", "nr", c.number));
  row.appendChild(name);
  if (c.favorite) row.appendChild(el("span", "star", "★"));
  if (c.group) row.appendChild(el("span", "grp", c.group));
  const call = el("button", "redial", "📞");
  row.appendChild(call);
  const dial = () => {
    if (bridge) bridge.place_call(c.number);
    switchView("dial");
  };
  call.addEventListener("click", (e) => { e.stopPropagation(); dial(); });
  row.addEventListener("click", dial);
  return row;
}

function renderContactList(boxId, searchId, favOnly) {
  const box = $(boxId);
  box.textContent = "";
  const q = ($(searchId).value || "").trim().toLowerCase();
  const rows = state.contacts.filter((c) =>
    (!favOnly || c.favorite)
    && (!q || (c.name || "").toLowerCase().includes(q) || (c.number || "").includes(q)));
  if (!rows.length) {
    box.appendChild(el("div", "empty",
      q ? "No matches." : (favOnly ? "No favorites yet." : "No contacts yet.")));
    return;
  }
  for (const c of rows) box.appendChild(buildContactRow(c));
}

function renderContacts() { renderContactList("contacts-list", "contacts-search", false); }
function renderFavorites() { renderContactList("favorites-list", "favorites-search", true); }

// ==========================================================================
// State entry point (called from Python via runJavaScript)
// ==========================================================================
window.nb = {
  apply(patch) {
    if (!patch) return;
    if ("calls" in patch) { state.calls = patch.calls || []; renderCalls(state.calls); }
    if ("accounts" in patch) { state.accounts = patch.accounts; renderAccounts(state.accounts); }
    if ("suppliers" in patch) { state.suppliers = patch.suppliers; renderSuppliers(state.suppliers); }
    if ("recents" in patch) { state.recents = patch.recents; renderRecents(state.recents); }
    if ("history" in patch) { state.history = patch.history || []; renderHistory(); }
    if ("contacts" in patch) {
      state.contacts = patch.contacts || [];
      renderContacts();
      renderFavorites();
    }
  },
};

// ==========================================================================
// Wire static controls
// ==========================================================================
function wireControls() {
  buildPad();

  $("btn-call").addEventListener("click", placeCall);
  $("num").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); placeCall(); }
  });

  // "View all" jumps to the in-app History tab (phase 2).
  $("view-all").addEventListener("click", () => switchView("history"));

  // Tab bar + view plumbing.
  for (const t of document.querySelectorAll("#tabbar .tab")) {
    t.addEventListener("click", () => switchView(t.dataset.view));
  }
  $("history-search").addEventListener("input", renderHistory);
  $("contacts-search").addEventListener("input", renderContacts);
  $("favorites-search").addEventListener("input", renderFavorites);
  $("contacts-manage").addEventListener("click", () => bridge && bridge.open_window("contacts"));

  // Chrome buttons
  $("btn-min").addEventListener("click", () => bridge && bridge.minimize());
  $("btn-close").addEventListener("click", () => bridge && bridge.close_win());
  $("btn-menu").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("app-menu", openAppMenu); });
  $("account-pill").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("account-menu", openAccountMenu); });
  $("supplier-sel").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("supplier-menu", openSupplierMenu); });
  for (const id of MENU_IDS) wireMenuKeys($(id));

  // Title-bar drag: mousedown on the chrome (but not on a button) hands the
  // move to the OS via bridge.start_move() (brief §web_shell).
  $("chrome").addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest("button, .account, .menu")) return;
    if (bridge) bridge.start_move();
  });
  // Double-click chrome toggles maximize/restore.
  $("chrome").addEventListener("dblclick", (e) => {
    if (e.target.closest("button, .account, .menu")) return;
    if (bridge) bridge.toggle_max_restore();
  });

  // Dismiss menus on any outside click / Escape; type-to-dial only while
  // no call is live (in-call digits belong to the DTMF pads) and only when
  // the keystroke isn't already headed into a text field (search boxes).
  document.addEventListener("click", closeMenus);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeMenus(); return; }
    const typing = e.target instanceof HTMLInputElement;
    if (e.key === "/" && !typing) {
      e.preventDefault();
      switchView("dial");
      $("num").focus();
    } else if (/^[0-9*#+]$/.test(e.key) && !typing && state.calls.length === 0) {
      switchView("dial");
      $("num").focus();
      $("num").value += e.key;
    }
  });
}

function toggleMenu(id, opener) {
  const m = $(id);
  if (!m.hidden) { m.hidden = true; return; }
  opener();
}

// ==========================================================================
// Boot: QWebChannel handshake
// ==========================================================================
function boot() {
  wireControls();
  if (typeof qt === "undefined" || !qt.webChannelTransport) {
    // Running the raw file without Qt (design preview) -- still interactive.
    return;
  }
  new QWebChannel(qt.webChannelTransport, (channel) => {
    bridge = channel.objects.bridge;
    bridge.ready();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
