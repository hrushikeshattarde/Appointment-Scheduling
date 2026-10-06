// Pickup appointments board, written for the account managers who follow their customers'
// pickups. Today says what needs a person and what is coming; All pickups is the full list; the
// Calendar shows the week; one pickup opens in a side panel that says where it stands and what
// to do. Admins also get the Access tab (/api/access).
//
// Kept plain on purpose. Every pickup sits in exactly one group (Needs you, Not asked yet, Asked
// vendor, Booked, Vendor declined, Canceled), so the numbers agree on every page, and the only
// colour that means something is a red dot on what needs a person. Every value from the server is
// placed with text nodes (vendor emails are untrusted text), never as HTML. With sign-in on, the
// server answers with the signed-in person's customers only; what this page hides is for looks,
// the server refuses.

const API = "/api/booking";
const view = document.getElementById("view");
const drawer = document.getElementById("drawer");
const scrim = document.getElementById("scrim");
const toastEl = document.getElementById("toast");
const meInput = document.getElementById("me");
const meLabel = document.getElementById("me-label");
const whoEl = document.getElementById("who");
const signoutForm = document.getElementById("signout");
const accessTab = document.getElementById("access-tab");
const attentionCount = document.getElementById("attention-count");
const customerSelect = document.getElementById("customer");
const stampEl = document.getElementById("stamp");

// Plain words for each status; the steps a pickup goes through.
const STATUS = {
  unscheduled: "Not asked yet",
  pending: "Asked vendor",
  scheduled: "Booked",
  declined: "Vendor declined",
  canceled: "Canceled",
};
const GROUPS = { attention: "Needs you", ...STATUS };
const STEPS = ["unscheduled", "pending", "scheduled"];
const SOURCE = { requested: "the time we asked for", confirmed: "confirmed by the vendor" };
const MESSAGE_KINDS = {
  request: "Booking request",
  reschedule: "Asked for a new time",
  follow_up: "Reminder",
  acknowledge: "Thank you",
  accept_offer: "Accepted the vendor's time",
  ask_alternative: "Asked for other days",
  answer_question: "Answered a question",
  escalate_to_customer: "Note to the customer",
  notify_customer_desk: "Note to the customer",
  reply: "Vendor's reply",
  customer_desk: "From the customer",
};
const READING = {
  confirmed: "Vendor confirmed",
  counter_offer: "Vendor offered another time",
  question: "Vendor asked a question",
  rejected: "Vendor said no",
  deferred: "Vendor will answer later",
};
// What the agent is doing on its own, in plain words.
const JOB = {
  waiting: "Waiting",
  planned: "Planned",
  blocked: "Needs a person first",
  held: "A person books this one",
  done: "Done",
  failed: "Did not work, will try again",
  canceled: "Not needed any more",
};
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
// Every time on the board is Eastern (EST, or EDT in summer), whatever the reader's own clock.
const EASTERN = "America/New_York";
const TABS = ["overview", "appointments", "week", "access"];
const LEVELS = { act: "View and act", view: "View", none: "No access" };

// Simple line icons (24 x 24, drawn with strokes); "|" separates paths.
const ICONS = {
  check: "M5 12.5l4.5 4.5L19 7.5",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z|M12 16v-5|M12 8h.01",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z|M20 20l-4-4",
};

// ------------------------------------------------------------------ small helpers

function load(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback; // private window or blocked storage: keep it in memory only
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* not remembered across visits, still used for this one */
  }
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "value") el.value = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function icon(name, size = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon${size ? ` ${size}` : ""}`);
  svg.setAttribute("aria-hidden", "true");
  for (const d of (ICONS[name] || "").split("|")) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

const pad = (n) => String(n).padStart(2, "0");
const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const pos = (row) => (row.po_numbers && row.po_numbers.length ? row.po_numbers.join(", ") : "-");
const show = (...parts) => parts.flat().filter(Boolean); // replaceChildren would print a null
const sentence = (text) => (text ? cap(text).replace(/\.*$/, ".") : "");

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// A pickup slot comes on the Eastern clock, written "YYYY-MM-DD" or "YYYY-MM-DD HH:MM"; it is
// shown as written, with its weekday and "ET".
function parseLocal(local) {
  if (!local) return null;
  const [day, time] = local.split(" ");
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return null;
  return { day, y, m, d, time: time || null, weekday: WEEKDAYS[new Date(y, m - 1, d).getDay()] };
}

function fmtSlot(local) {
  const p = parseLocal(local);
  if (!p) return "No date yet";
  return `${p.weekday} ${pad(p.m)}/${pad(p.d)}${p.time ? ` ${p.time} ET` : ""}`;
}

// "Today · Tue 10/06", "Tomorrow · Wed 10/07", else "Thu 10/08".
function dayName(day) {
  const today = isoDay(startOfToday());
  if (day === today) return `Today · ${fmtSlot(day)}`;
  if (day === isoDay(addDays(startOfToday(), 1))) return `Tomorrow · ${fmtSlot(day)}`;
  return fmtSlot(day);
}

// Instants (sent, raised, delivery) are UTC on the wire and shown on the Eastern clock.
function fmtInstant(iso) {
  if (!iso) return "";
  const text = new Date(iso).toLocaleString("en-US", {
    timeZone: EASTERN,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  return `${text} ET`;
}

// The Eastern clock now: its calendar day as a local Date at midnight, and its hour.
function easternNow() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: EASTERN,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date())
      .map((part) => [part.type, part.value]),
  );
  return { day: new Date(Number(parts.year), Number(parts.month) - 1, Number(parts.day)), hour: Number(parts.hour), minute: Number(parts.minute) };
}

function ago(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function startOfToday() {
  return easternNow().day; // the Eastern calendar day, so "today" is the same for everyone
}

function mondayOf(date) {
  const d = new Date(date);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d;
}

function addDays(date, n) {
  const d = new Date(date);
  d.setDate(d.getDate() + n);
  return d;
}

let toastTimer;
function toast(text, bad = false) {
  toastEl.textContent = text;
  toastEl.className = `toast show${bad ? " bad" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.className = "toast";
  }, 3800);
}

function signIn() {
  location.href = `/auth/login${query({ next: location.pathname + location.hash })}`;
}

async function request(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) {
    signIn(); // the sign-in ran out (or never happened): Google, then straight back here
    throw new Error("Signing you in…");
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new Error(cap(detail));
  }
  return res.json();
}

const api = (path, options) => request(API + path, options);

function query(params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined && value !== false) q.set(key, value);
  }
  const text = q.toString();
  return text ? `?${text}` : "";
}

// ------------------------------------------------------------------ where a pickup stands

// A pickup needs a person when it has an open to-do, or its time passed and nobody booked it.
const needsYou = (row) => row.status !== "canceled" && (row.past_due || row.open_exceptions.length > 0);
const groupOf = (row) => (needsYou(row) ? "attention" : row.status);

// Earliest pickup first; pickups with no date yet last.
const byPickup = (a, b) =>
  (a.pickup_local ? 0 : 1) - (b.pickup_local ? 0 : 1) ||
  (a.pickup_local || "").localeCompare(b.pickup_local || "") ||
  a.id - b.id;

// What the person has to do, in a few words: the first to-do, and how many more.
function needText(row) {
  const labels = row.open_exceptions.map((e) => e.label);
  if (row.past_due && !row.open_exceptions.some((e) => e.kind === "pickup_expired")) {
    labels.unshift("Pickup time passed, not booked");
  }
  if (!labels.length) return "";
  return labels.length > 1 ? `${labels[0]} (+${labels.length - 1} more)` : labels[0];
}

const statusText = (status) =>
  h("span", { class: "status" }, status === "scheduled" ? icon("check", "sm") : null, STATUS[status] || cap(status));

// The status, and under it, with the red dot, what the person has to do (when anything).
const statusCell = (row) => [statusText(row.status), needsYou(row) ? h("div", { class: "need" }, needText(row)) : null];

// ------------------------------------------------------------------ state and routing

const defaultFilters = () => ({ group: "", exception: "", q: "", start: "", end: "" });

const state = {
  tab: "overview",
  renderedTab: null,
  caseId: null,
  customer: load("fp.customer", ""),
  me: load("fp.me", ""),
  filters: defaultFilters(),
  moreFilters: false,
  weekStart: mondayOf(startOfToday()),
  kinds: [],
  references: [],
  refocus: null,
  summaryOpen: false,
  viewer: { signed_in: false, admin: true, customers: [] }, // until /api/me answers
};

function parseHash() {
  const [tab, id] = location.hash.replace(/^#/, "").split("/");
  const known = TABS.includes(tab) && (tab !== "access" || state.viewer.admin);
  return { tab: known ? tab : "overview", caseId: id && tab !== "access" ? Number(id) : null };
}

function go(tab, caseId = null) {
  location.hash = caseId ? `${tab}/${caseId}` : tab;
}

async function route() {
  const { tab, caseId } = parseHash();
  state.tab = tab;
  for (const link of document.querySelectorAll(".tabs a")) {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (state.renderedTab !== tab) await renderTab();
  if (caseId) await openCase(caseId);
  else closeDrawer(false);
}

function showList(filter) {
  state.filters = { ...defaultFilters(), ...filter };
  state.moreFilters = Boolean(filter.exception || filter.start || filter.end);
  if (state.tab === "appointments") renderTab();
  else go("appointments");
}

function setFilter(patch, refocus = null) {
  Object.assign(state.filters, patch);
  state.refocus = refocus;
  renderTab();
}

// The red number next to Today: pickups that need a person, shown on every page.
function showAttention(n) {
  attentionCount.textContent = n ? String(n) : "";
  attentionCount.hidden = !n;
  attentionCount.title = n ? `${plural(n, "pickup")} need you` : "";
}

async function refreshAttention() {
  try {
    showAttention((await api(`/cases${query({ customer: state.customer })}`)).filter(needsYou).length);
  } catch {
    /* the badge is a convenience; the page itself says what failed */
  }
}

async function renderTab() {
  state.renderedTab = state.tab;
  try {
    if (state.tab === "overview") await renderOverview();
    else if (state.tab === "appointments") await renderAppointments();
    else if (state.tab === "access") await renderAccess();
    else await renderWeek();
    if (state.tab !== "overview") refreshAttention();
    const now = easternNow();
    stampEl.textContent = `Updated ${pad(now.hour)}:${pad(now.minute)} ET`;
  } catch (err) {
    view.replaceChildren(
      h(
        "section",
        { class: "box" },
        h("div", { class: "error" }, `The board could not load: ${err.message}`),
        h("div", { class: "empty" }, h("button", { class: "btn", type: "button", onclick: renderTab }, "Try again")),
      ),
    );
  }
}

// ------------------------------------------------------------------ shared pieces

function box(title, count, children, hint = null) {
  return h(
    "section",
    { class: "box" },
    h("header", {}, h("h2", {}, title), count !== null ? h("span", { class: "n" }, count) : null, hint ? h("span", { class: "hint" }, hint) : null),
    h("div", {}, children),
  );
}

const empty = (text, good = false) => h("div", { class: "empty" }, good ? icon("check") : null, text);

// ------------------------------------------------------------------ today

function greeting() {
  const first = (state.viewer.name || "").trim().split(/\s+/)[0];
  if (!first) return "Today";
  const hour = easternNow().hour;
  return `Good ${hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening"}, ${first}`;
}

async function renderOverview() {
  const scope = query({ customer: state.customer });
  const [data, all, summary] = await Promise.all([
    api(`/overview${scope}`),
    api(`/cases${scope}`),
    state.summaryOpen ? api(`/today${scope}`) : null,
  ]);
  const live = all.filter((r) => r.status !== "canceled");
  const attention = live.filter(needsYou).sort(byPickup);
  const inGroup = (g) => live.filter((r) => groupOf(r) === g);
  const drafts = inGroup("unscheduled").filter((r) => r.draft_ready).length;
  const bookedSoon = data.upcoming.filter((r) => groupOf(r) === "scheduled").length;
  const several = new Set(all.map((r) => r.customer)).size > 1;
  const mail = data.mail || [];
  showAttention(attention.length);

  const stat = (g, n, sub) =>
    h(
      "button",
      { class: `stat${n ? (g === "attention" ? " alert" : "") : " zero"}`, type: "button", onclick: () => showList({ group: g }) },
      h("div", { class: "label" }, GROUPS[g]),
      h("div", { class: "num" }, n),
      h("div", { class: "sub" }, sub),
    );
  const whose = state.customer || (state.viewer.admin ? "All customers" : "Your customers");
  const today = new Date().toLocaleDateString("en-US", { timeZone: EASTERN, weekday: "long", month: "long", day: "numeric" });
  view.replaceChildren(
    ...show(
      h(
        "div",
        { class: "page-head" },
        h("div", {}, h("h1", {}, greeting()), h("p", { class: "sub" }, `${today} · ${whose}`)),
        h(
          "div",
          { class: "acts" },
          h(
            "button",
            { class: "btn", type: "button", "aria-expanded": String(state.summaryOpen), onclick: toggleSummary },
            state.summaryOpen ? "Hide daily summary" : "Daily summary",
          ),
        ),
      ),
      summary ? summaryBox(summary) : null,
      h(
        "section",
        { class: "stats", "aria-label": "Totals" },
        stat("attention", attention.length, "Only a person can decide"),
        stat("unscheduled", inGroup("unscheduled").length, drafts ? `${plural(drafts, "email")} ready to send` : "Request not sent yet"),
        stat("pending", inGroup("pending").length, "Waiting for their answer"),
        stat("scheduled", inGroup("scheduled").length, `${bookedSoon} in the next ${data.days} days`),
      ),
      h(
        "div",
        { class: "cols" },
        box(
          "Needs you",
          attention.length,
          attention.length ? attention.map((r) => attentionRow(r, several)) : empty("Nothing needs you right now.", true),
          attention.length ? "Open one to see what to do" : null,
        ),
        box(
          `Coming up, next ${data.days} days`,
          data.upcoming.length,
          data.upcoming.length ? upcomingList(data.upcoming) : empty("No pickups in the next days."),
          "Times are Eastern (ET)",
        ),
      ),
      mail.length ? mailBox(mail, live) : null,
      helpBox(),
    ),
  );
}

function attentionRow(row, several) {
  return h(
    "button",
    { class: "item", type: "button", onclick: () => go(state.tab, row.id) },
    h("span", { class: "dot", "aria-hidden": "true" }),
    h("span", { class: "name" }, row.vendor || "Unknown vendor"),
    h("span", { class: "when" }, fmtSlot(row.pickup_local)),
    h("span", { class: "what" }, [needText(row), `PO ${pos(row)}`, several ? row.customer : null].filter(Boolean).join(" · ")),
    h("span", { class: "go" }, "Open"),
  );
}

function upcomingList(rows) {
  const out = [];
  let last = null;
  for (const row of rows) {
    if (row.pickup_date !== last) {
      last = row.pickup_date;
      out.push(h("div", { class: "day-head" }, dayName(row.pickup_date)));
    }
    out.push(
      h(
        "button",
        { class: "up", type: "button", onclick: () => go(state.tab, row.id) },
        h("span", { class: "time" }, parseLocal(row.pickup_local)?.time || "Any time"),
        h("span", { class: "name" }, row.vendor || "Unknown vendor"),
        needsYou(row)
          ? h("span", { class: "state alert" }, h("span", { class: "dot", "aria-hidden": "true" }), "Needs you")
          : h("span", { class: "state" }, statusText(row.status)),
        h("span", { class: "po" }, `PO ${pos(row)}`),
      ),
    );
  }
  return out;
}

// Emails the agent could not tie to any pickup: kept so none is lost. A person links each to its
// pickup (the agent then reads it as that pickup's reply) or dismisses it.
function mailBox(items, pickups) {
  return box("Emails we could not match", items.length, items.map((m) => mailItem(m, pickups)), "Link each to its pickup, or dismiss it");
}

function mailItem(m, pickups) {
  const first = (m.body || "").split("\n").map((line) => line.trim()).find(Boolean) || "";
  const choose = h(
    "select",
    { "aria-label": "The pickup this email is about" },
    h("option", { value: "" }, "Choose the pickup…"),
    pickups.map((r) => h("option", { value: r.id }, `#${r.id} ${r.vendor || "Unknown vendor"} · PO ${pos(r)} · ${fmtSlot(r.pickup_local)}`)),
  );
  const link = async () => {
    const by = decisionBy();
    if (!by) return;
    if (!choose.value) {
      toast("Choose the pickup this email is about first.", true);
      choose.focus();
      return;
    }
    try {
      await api(`/mail/${m.id}/link`, { method: "POST", body: JSON.stringify({ case_id: Number(choose.value), ...by }) });
      toast(`Linked to pickup #${choose.value}; the agent read it`);
      renderTab();
    } catch (err) {
      toast(err.message, true);
    }
  };
  const dismiss = async () => {
    const by = decisionBy();
    if (!by) return;
    const note = window.prompt("Why does this email need nothing?", "not about a pickup");
    if (!note) return;
    try {
      await api(`/mail/${m.id}/dismiss`, { method: "POST", body: JSON.stringify({ note, ...by }) });
      toast("Dismissed");
      renderTab();
    } catch (err) {
      toast(err.message, true);
    }
  };
  return h(
    "div",
    { class: "mail-item" },
    h("div", { class: "title" }, m.subject || "(no subject)"),
    h("div", { class: "meta" }, `${m.from || "Unknown sender"} · ${fmtInstant(m.sent_at)}`),
    first ? h("div", { class: "snippet" }, first.slice(0, 220)) : null,
    h(
      "div",
      { class: "acts" },
      choose,
      h("button", { class: "btn small primary", type: "button", onclick: link }, "Link to pickup"),
      h("button", { class: "btn small link", type: "button", onclick: dismiss }, "Dismiss"),
    ),
  );
}

// The morning summary as text, ready to paste into an email or a chat.
function summaryBox(summary) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(summary.text);
      toast("Summary copied: paste it into an email or a chat");
    } catch {
      toast("Could not copy here; select the text instead.", true);
    }
  };
  return h(
    "section",
    { class: "box summary", "aria-label": "Daily summary" },
    h(
      "header",
      {},
      h("h2", {}, `Daily summary · ${summary.local_time}`),
      h("button", { class: "btn small primary", type: "button", onclick: copy }, "Copy"),
    ),
    h("pre", {}, summary.text),
  );
}

function toggleSummary() {
  state.summaryOpen = !state.summaryOpen;
  renderTab();
}

function helpBox() {
  const step = (title, text) => h("li", {}, h("b", {}, title), ` ${text}`);
  return h(
    "details",
    { class: "section help" },
    h("summary", {}, "How booking works"),
    h(
      "ol",
      {},
      step("We ask the vendor.", "The agent emails the vendor's booking desk for a pickup time."),
      step("The vendor answers.", "By replying, or by clicking a time in our email."),
      step("You check it.", "Anything only a person can decide shows under Needs you, with a red dot."),
      step("Booked.", "The pickup time is set."),
    ),
  );
}

// ------------------------------------------------------------------ all pickups

async function renderAppointments() {
  const f = state.filters;
  const all = await api(
    `/cases${query({ customer: state.customer, exception: f.exception, q: f.q, start: f.start, end: f.end })}`,
  );
  const rows = all.filter((r) => !f.group || groupOf(r) === f.group);
  const counts = Object.fromEntries(Object.keys(GROUPS).map((g) => [g, all.filter((r) => groupOf(r) === g).length]));
  // Declined and Canceled are rare: their tab shows only when there is something in it.
  const tabs = [["", "All", all.length], ...Object.entries(GROUPS).map(([g, label]) => [g, label, counts[g]])].filter(
    ([g, , n]) => n || !["declined", "canceled"].includes(g) || f.group === g,
  );
  const groupTabs = h(
    "div",
    { class: "groups", role: "group", "aria-label": "Show" },
    tabs.map(([g, label, n]) =>
      h(
        "button",
        { type: "button", class: g === "attention" && n ? "alert" : null, "aria-pressed": String(f.group === g), onclick: () => setFilter({ group: g }) },
        label,
        h("span", { class: "n" }, n),
      ),
    ),
  );
  const search = h("input", {
    type: "search",
    placeholder: "Search by PO, load, vendor or pickup number",
    "aria-label": "Search",
    value: f.q,
  });
  search.addEventListener(
    "input",
    debounce(() => setFilter({ q: search.value }, "search"), 300),
  );
  const filtered = Boolean(f.exception || f.start || f.end || f.q || f.group);
  const kindSelect = h(
    "select",
    { "aria-label": "What is needed", onchange: (e) => setFilter({ exception: e.target.value }) },
    h("option", { value: "" }, "Anything"),
    state.kinds.map((k) => h("option", { value: k.kind }, k.label)),
  );
  kindSelect.value = f.exception;
  const more = h(
    "div",
    { class: "filters" },
    h("label", {}, "What is needed", kindSelect),
    h("label", {}, "Pickup from", h("input", { type: "date", value: f.start, onchange: (e) => setFilter({ start: e.target.value }) })),
    h("label", {}, "to", h("input", { type: "date", value: f.end, onchange: (e) => setFilter({ end: e.target.value }) })),
  );
  more.hidden = !state.moreFilters;
  const several = new Set(all.map((r) => r.customer)).size > 1;
  const headers = ["Pickup", "Vendor", "Status", "PO", "Pickup number", ...(several ? ["Customer"] : [])];
  const table = rows.length
    ? h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "pickups" },
          h("thead", {}, h("tr", {}, headers.map((t) => h("th", { scope: "col" }, t)))),
          h("tbody", {}, rows.map((r) => tableRow(r, several))),
        ),
      )
    : h("section", { class: "box" }, empty(filtered ? "No pickups match. Try Clear to see them all." : "No pickups yet."));
  view.replaceChildren(
    h(
      "div",
      { class: "page-head" },
      h("div", {}, h("h1", {}, "All pickups"), h("p", { class: "sub" }, "Click a pickup to see where it stands and what to do. Times are Eastern (ET).")),
    ),
    h(
      "div",
      { class: "finder" },
      h("div", { class: "search" }, icon("search"), search),
      h(
        "button",
        {
          class: "btn",
          type: "button",
          "aria-expanded": String(state.moreFilters),
          onclick: () => {
            state.moreFilters = !state.moreFilters;
            renderTab();
          },
        },
        state.moreFilters ? "Hide filters" : "Filters",
      ),
      filtered ? h("button", { class: "btn link", type: "button", onclick: () => setFilter(defaultFilters()) }, "Clear") : null,
    ),
    more,
    groupTabs,
    table,
  );
  if (state.refocus === "search") {
    search.focus();
    search.setSelectionRange(search.value.length, search.value.length);
  }
  state.refocus = null;
}

function tableRow(row, several) {
  const open = () => go("appointments", row.id);
  return h(
    "tr",
    {
      tabindex: "0",
      onclick: open,
      onkeydown: (e) => {
        if (e.key === "Enter") open();
      },
    },
    h(
      "td",
      { class: "nowrap mono", "data-label": "Pickup" },
      h("span", { class: "strong" }, fmtSlot(row.pickup_local)),
      h("div", { class: "sub" }, cap(SOURCE[row.pickup_source] || row.pickup_source)),
    ),
    h("td", { "data-label": "Vendor" }, h("span", { class: "strong" }, row.vendor || "Unknown vendor"), h("div", { class: "sub" }, row.vendor_city || "")),
    h("td", { "data-label": "Status" }, statusCell(row)),
    h("td", { class: "mono", "data-label": "PO" }, pos(row)),
    h("td", { class: "mono", "data-label": "Pickup #" }, row.pickup_number || h("span", { class: "muted" }, "-")),
    several ? h("td", { "data-label": "Customer" }, row.customer || "") : null,
  );
}

// ------------------------------------------------------------------ calendar

async function renderWeek() {
  const start = state.weekStart;
  const days = [...Array(7).keys()].map((i) => addDays(start, i));
  const rows = await api(
    `/cases${query({ customer: state.customer, start: isoDay(days[0]), end: isoDay(days[6]) })}`,
  );
  const today = isoDay(startOfToday());
  const shift = (n) => {
    state.weekStart = addDays(state.weekStart, n);
    renderTab();
  };
  view.replaceChildren(
    h(
      "div",
      { class: "week-head" },
      h(
        "div",
        {},
        h("h1", {}, "Calendar"),
        h("p", { class: "sub" }, `Week of ${fmtSlot(isoDay(start))} · ${plural(rows.length, "pickup")} · times are Eastern (ET)`),
      ),
      h(
        "div",
        { class: "nav" },
        h("button", { class: "btn small", type: "button", onclick: () => shift(-7) }, "← Previous"),
        h(
          "button",
          {
            class: "btn small",
            type: "button",
            onclick: () => {
              state.weekStart = mondayOf(startOfToday());
              renderTab();
            },
          },
          "This week",
        ),
        h("button", { class: "btn small", type: "button", onclick: () => shift(7) }, "Next →"),
      ),
    ),
    h(
      "div",
      { class: "week" },
      days.map((day) => {
        const key = isoDay(day);
        const items = rows.filter((r) => r.pickup_date === key);
        return h(
          "section",
          { class: `day${key === today ? " today" : ""}`, "aria-label": fmtSlot(key) },
          h("header", {}, key === today ? `Today · ${fmtSlot(key)}` : fmtSlot(key), h("span", { class: "n" }, items.length || "")),
          h("div", { class: "items" }, items.length ? items.map(card) : h("div", { class: "none" }, "No pickups")),
        );
      }),
    ),
  );
}

function card(row) {
  return h(
    "button",
    { class: "card", type: "button", title: row.stage, onclick: () => go("week", row.id) },
    h("div", { class: "t" }, parseLocal(row.pickup_local)?.time || "Any time"),
    h("div", { class: "v" }, row.vendor || "Unknown vendor"),
    needsYou(row) ? h("div", { class: "need" }, "Needs you") : h("div", { class: "s" }, STATUS[row.status] || cap(row.status)),
  );
}

// ------------------------------------------------------------------ one pickup (side panel)

let lastFocus = null;

const closeButton = () =>
  h("button", { class: "btn small", type: "button", "aria-label": "Close", onclick: () => closeDrawer() }, "Close");

async function openCase(id) {
  state.caseId = id;
  if (drawer.hidden) lastFocus = document.activeElement;
  drawer.hidden = false;
  scrim.hidden = false;
  try {
    renderDrawer(await api(`/cases/${id}`));
  } catch (err) {
    drawer.replaceChildren(
      h("div", { class: "head" }, h("div", { class: "top" }, h("h2", { id: "case-title" }, `Pickup ${id}`), closeButton())),
      h("div", { class: "content" }, h("div", { class: "error" }, err.message)),
    );
  }
}

function closeDrawer(updateHash = true) {
  if (drawer.hidden) return;
  drawer.hidden = true;
  scrim.hidden = true;
  state.caseId = null;
  if (updateHash) go(state.tab);
  if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
}

let drawnCase = null;

function renderDrawer(d) {
  const sameCase = drawnCase === d.id;
  drawnCase = d.id;
  const head = h(
    "div",
    { class: "head" },
    h("div", { class: "top" }, h("h2", { id: "case-title", tabindex: "-1" }, d.vendor || "Unknown vendor"), closeButton()),
    h("div", { class: "ids" }, [d.vendor_city, `PO ${pos(d)}`, d.customer].filter(Boolean).join(" · ")),
    progress(d.status),
  );
  drawer.replaceChildren(
    head,
    h("div", { class: "content" }, ...show(happening(d), whatToDo(d), keyFacts(d), messagesSection(d), timelineSection(d), moreDetails(d))),
  );
  if (!sameCase) drawer.scrollTop = 0;
  drawer.querySelector("#case-title").focus({ preventScroll: sameCase });
}

// Not asked yet → Asked vendor → Booked, with the step it is on in bold.
function progress(status) {
  const at = status === "declined" ? 1 : STEPS.indexOf(status);
  const parts = [];
  STEPS.forEach((name, i) => {
    if (i) parts.push(h("span", { "aria-hidden": "true" }, "→"));
    const done = at > i || status === "scheduled";
    parts.push(h("span", { class: at === i ? "now" : done ? "done" : null }, done ? "✓ " : "", STATUS[name]));
  });
  if (status === "declined" || status === "canceled") parts.push(h("span", { class: "now" }, `· ${STATUS[status]}`));
  return h("div", { class: "progress", "aria-label": `Progress: ${STATUS[status] || status}` }, parts);
}

// One sentence: where it stands and whose move it is.
function happening(d) {
  return h(
    "div",
    { class: "section now-box" },
    h("div", { class: "label" }, "What's happening"),
    h("div", { class: "say" }, sentence(d.stage)),
    d.past_due ? h("div", { class: "late" }, "The pickup time has passed and it is not booked.") : null,
  );
}

// The name recorded with each decision, when there is no sign-in. It is asked for in the top
// bar and again inside the panel, which covers the top bar while it is open; both stay in step.
function setMe(value) {
  state.me = value;
  save("fp.me", value);
  for (const input of [meInput, ...document.querySelectorAll(".me-inline")]) {
    if (input.value !== value) input.value = value;
  }
}

function nameField() {
  if (state.viewer.signed_in) return null; // recorded under the signed-in name
  const input = h("input", {
    class: "me-inline",
    type: "text",
    value: state.me,
    placeholder: "your name",
    autocomplete: "name",
    size: "12",
    "aria-label": "Your name, recorded with the decision",
  });
  input.addEventListener("input", () => setMe(input.value));
  return h("label", { class: "toggle me-field" }, "Your name", input);
}

// Everything a person can do on this pickup, in one place: each open to-do with its own
// buttons, then booking it by hand or canceling it.
function whatToDo(d) {
  const open = d.exceptions.filter((e) => e.open);
  const section = h("div", { class: "section" }, h("h3", {}, open.length > 1 ? `What to do (${open.length})` : "What to do"));
  if (open.length) {
    section.append(h("div", {}, open.map((e) => todoItem(d, e))));
  } else {
    let text = "Nothing for you right now. The agent is handling it.";
    if (d.status === "scheduled") text = "Nothing. This pickup is booked.";
    else if (d.status === "canceled") text = "Nothing. This pickup is canceled.";
    else if (d.past_due) text = "The pickup time passed and nobody booked it. If the truck picked up, mark it booked; if it is no longer needed, cancel it.";
    section.append(h("div", { class: "nothing" }, d.past_due ? null : icon("check"), text));
  }
  if (d.can_act === false) {
    section.append(
      h("div", { class: "view-only" }, icon("info"), "You can follow this customer's pickups. To approve, book or cancel, ask an admin for View and act."),
    );
    return section;
  }
  const canceled = d.status === "canceled";
  section.append(
    h(
      "div",
      { class: "actionbar" },
      h(
        "button",
        { class: "btn", type: "button", disabled: canceled, onclick: () => toggleForm(section, bookedForm(d)) },
        d.status === "scheduled" ? "Change the booking…" : "I booked it myself…",
      ),
      h(
        "button",
        { class: "btn danger", type: "button", disabled: canceled, onclick: () => toggleForm(section, cancelForm(d)) },
        "Cancel this pickup…",
      ),
      nameField(),
    ),
  );
  return section;
}

function todoItem(d, e) {
  const item = h(
    "div",
    { class: "todo" },
    h("div", { class: "k" }, e.label),
    h("div", { class: "d" }, sentence(e.description)),
    e.hint ? h("div", { class: "how" }, h("b", {}, "How: "), e.hint) : null,
    h("div", { class: "by" }, `Raised by ${e.raised_by} · ${fmtInstant(e.raised_at)} (${ago(e.raised_at)})`),
  );
  if (d.can_act === false) return item; // view only: the to-do is shown, not worked
  const acts = h("div", { class: "acts" });
  if (e.kind === "confirmation_review" && d.can_approve) {
    acts.append(h("button", { class: "btn primary small", type: "button", onclick: () => approveCase(d) }, "Approve the vendor's time"));
  }
  if (e.kind === "missing_reference") {
    acts.append(h("button", { class: "btn primary small", type: "button", onclick: () => toggleForm(item, referenceForm(d, e)) }, "Add the number…"));
  }
  acts.append(h("button", { class: "btn small", type: "button", onclick: () => toggleForm(item, resolveForm(d, e)) }, "Mark as handled…"));
  item.append(acts);
  return item;
}

function toggleForm(container, form) {
  const existing = container.querySelector(":scope > form");
  if (existing) {
    existing.remove();
    if (existing.dataset.kind === form.dataset.kind) return;
  }
  container.append(form);
  const first = form.querySelector("input, select, textarea");
  if (first) first.focus();
}

function formButtons(form, label, kind = "primary") {
  return h(
    "div",
    { class: "acts" },
    h("button", { class: `btn ${kind} small`, type: "submit" }, label),
    h("button", { class: "btn small", type: "button", onclick: () => form.remove() }, "Back"),
  );
}

function resolveForm(d, e) {
  const note = h("textarea", { required: true, placeholder: "What you did, for example: called the vendor and they confirmed" });
  const form = h("form", { class: "form" }, h("label", { class: "full" }, "What did you do about it?", note));
  form.dataset.kind = `resolve-${e.kind}`;
  form.append(formButtons(form, "Mark as handled"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(d.id, "resolve", { kind: e.kind, note: note.value }, `${e.label}: handled`);
  });
  return form;
}

// A number the desk needs that the load does not carry (the customer's shipment or SO number).
function referenceForm(d, e) {
  const missing = (e.detail && e.detail.missing) || [];
  const known = state.references.length ? state.references : missing.map((kind) => ({ kind, name: kind }));
  const kinds = known.filter((r) => !missing.length || missing.includes(r.kind));
  const kind = h("select", {}, kinds.map((r) => h("option", { value: r.kind }, cap(r.name))));
  const value = h("input", { type: "text", required: true, placeholder: "the number, as the customer gave it" });
  const form = h(
    "form",
    { class: "form" },
    h("div", { class: "fields" }, h("label", {}, "Which number", kind), h("label", {}, "Number", value)),
  );
  form.dataset.kind = "reference";
  form.append(formButtons(form, "Add to the pickup"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(d.id, "reference", { kind: kind.value, value: value.value }, "Number added");
  });
  return form;
}

function bookedForm(d) {
  const slot = parseLocal(d.confirmed_local || d.requested_local);
  const via = h(
    "select",
    {},
    [
      ["phone", "By phone"],
      ["portal", "On the vendor's website"],
      ["email", "By email"],
      ["other", "Another way"],
    ].map(([v, label]) => h("option", { value: v }, label)),
  );
  const date = h("input", { type: "date", value: slot ? slot.day : "" });
  const time = h("input", { type: "time", value: slot && slot.time ? slot.time : "" });
  const number = h("input", { type: "text", value: d.pickup_number || "", placeholder: "for example 20463798" });
  const desk = h("input", { type: "text", value: d.desk || "", placeholder: "email, phone or website you booked with" });
  const note = h("input", { type: "text", placeholder: "optional" });
  const form = h(
    "form",
    { class: "form" },
    h(
      "div",
      { class: "fields" },
      h("label", {}, "How did you book it?", via),
      h("label", {}, "Vendor's pickup number", number),
      h("label", {}, "Pickup date", date),
      h("label", {}, "Pickup time (Eastern)", time),
      h("label", { class: "full" }, "Booked with (we remember it for this vendor)", desk),
      h("label", { class: "full" }, "Note", note),
    ),
  );
  form.dataset.kind = "booked";
  form.append(formButtons(form, "Save as booked"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(
      d.id,
      "booked",
      {
        via: via.value,
        date: date.value || null,
        time: time.value || null,
        pickup_number: number.value || null,
        note: note.value || null,
        desk: desk.value || null,
      },
      "Saved: the pickup is booked",
    );
  });
  return form;
}

function cancelForm(d) {
  const reason = h("textarea", { required: true, placeholder: "Why, for example: the customer canceled the load" });
  const form = h("form", { class: "form" }, h("label", { class: "full" }, "Why is this pickup no longer needed?", reason));
  form.dataset.kind = "cancel";
  form.append(formButtons(form, "Cancel this pickup", "danger"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(d.id, "cancel", { reason: reason.value }, "Pickup canceled");
  });
  return form;
}

// The few facts everyone needs, big and first.
function keyFacts(d) {
  const fact = (k, v, sub = null, big = false) =>
    h("div", {}, h("div", { class: "k" }, k), h("div", { class: `v${big ? " big" : ""}` }, v, sub ? h("span", { class: "sub" }, sub) : null));
  const delivery = [d.delivery_ref, d.delivery_at ? fmtInstant(d.delivery_at) : null].filter(Boolean).join(" · ");
  const desk = d.desk ? `${d.desk}${d.method ? ` (${d.method.replace("_", " ")})` : ""}` : d.method ? cap(d.method.replace("_", " ")) : "None on file yet";
  return h(
    "div",
    { class: "section" },
    h("h3", {}, "Pickup details"),
    h(
      "div",
      { class: "keyfacts" },
      fact("Pickup", fmtSlot(d.pickup_local), `${cap(SOURCE[d.pickup_source] || d.pickup_source)}, Eastern time`, true),
      fact("Vendor's pickup number", d.pickup_number || "Not given yet"),
      fact("Delivery", d.delivery_site || "-", delivery || null),
      fact("Booking desk", desk),
      fact("PO", pos(d)),
      fact("Customer", d.customer || "-"),
    ),
  );
}

// Every number on the case and where it came from: "PU# 20463798 (from the vendor, 10/01)".
// The load number heads the facts already, so it is left out here.
function numbersText(refs, current) {
  const rows = (refs || []).filter((r) => r.current === current && r.kind !== "load_number");
  if (!rows.length) return "-";
  return rows
    .map((r) => {
      const when = (current ? r.at : r.replaced_at) ? fmtInstant(current ? r.at : r.replaced_at).split(",")[0] : "";
      const how = current ? r.said : `replaced ${when}`;
      return `${r.label} ${r.value} (${how}${current && when ? `, ${when}` : ""})`;
    })
    .join("; ");
}

// How the vendor was booked before: "email shipping@vendor.example, 3 times, last 10/01".
function deskHistory(rows) {
  if (!rows || !rows.length) return "-";
  return rows
    .map((r) => {
      const last = r.last_worked_at ? `, last ${fmtInstant(r.last_worked_at).split(",")[0]}` : "";
      return `${r.method.replace("_", " ")}${r.desk ? ` ${r.desk}` : ""}, ${plural(r.worked_count, "time")}${last}`;
    })
    .join("; ");
}

// The times the latest request offered by link and what became of them:
// "Thu 10/01 08:00, 09:00, 10:00 · picked Thu 10/01 10:00".
const OFFER_STATES = {
  open: "waiting for the vendor to click",
  superseded: "replaced by a later request",
  closed: "pickup settled",
  expired: "link expired",
};
function offerText(offers) {
  if (!offers || !offers.length) return null;
  const o = offers[offers.length - 1];
  const times = o.slots.map((s, i) => (i ? s.split(" ").slice(2).join(" ") || s : s)).join(", ");
  const answer = o.answer ? (o.answer.startsWith("proposed") ? o.answer : `vendor picked ${o.answer}`) : OFFER_STATES[o.state] || o.state;
  return `${times} · ${answer}`;
}

// What the agent planned on its own: "Planned: rule 'vendor pickups': draft at Thu 10/01 10:00".
function jobText(jobs, kind = "request") {
  const job = (jobs || []).filter((j) => j.kind === kind).pop();
  if (!job) return null;
  if (kind === "tpro_write" && job.status === "planned" && /WRITEBACK/.test(job.reason || "")) {
    return "Not sent to Transport Pro automatically (that is switched off): enter the time there by hand.";
  }
  return `${JOB[job.status] || cap(job.status)}${job.reason ? `: ${job.reason}` : ""}`;
}

function moreDetails(d) {
  const rows = [
    ["We asked for", d.requested_local ? fmtSlot(d.requested_local) : "-"],
    ["Why that time", d.requested_why || "-"],
    ["Vendor confirmed", d.confirmed_local ? fmtSlot(d.confirmed_local) : "-"],
    ...(offerText(d.offers) ? [["Times offered by link", offerText(d.offers)]] : []),
    ...(jobText(d.jobs) ? [["The agent's plan", jobText(d.jobs)]] : []),
    ...(jobText(d.jobs, "tpro_write") ? [["Transport Pro", jobText(d.jobs, "tpro_write")]] : []),
    ["Numbers", numbersText(d.references, true)],
    ["Earlier numbers", numbersText(d.references, false)],
    ["Booked before with", deskHistory(d.desk_history)],
    ["Time on the tender", d.tendered_pickup_at ? fmtInstant(d.tendered_pickup_at) : "-"],
    ["Miles", d.miles ?? "-"],
    ["Times rescheduled", d.reschedule_count],
    ["Load number", d.load_id],
    ["Vendor's time zone", d.timezone || "-"],
    ["Pickup record", `#${d.id}`],
  ];
  return h(
    "details",
    { class: "section" },
    h("summary", {}, "More details"),
    h("dl", { class: "facts" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))])),
  );
}

function messagesSection(d) {
  const items = d.messages.map((m) => {
    const out = m.direction === "out";
    let when = fmtInstant(m.created_at);
    if (m.sent_at) when = fmtInstant(m.sent_at);
    else if (out) when = "not sent yet";
    const r = m.reading || {};
    let reading = null;
    if (!out && r.skipped) reading = `Not read as an answer: ${r.skipped}`;
    else if (!out && r.status) {
      const bits = [r.pickup_date, r.pickup_time, r.pickup_number ? `pickup number ${r.pickup_number}` : null].filter(Boolean);
      reading = `${READING[r.status] || cap(r.status.replace("_", " "))}${bits.length ? ` · ${bits.join(" ")}` : ""}`;
    }
    return h(
      "div",
      { class: "msg" },
      h(
        "div",
        { class: "line" },
        h("span", { class: "dir" }, out ? "Sent" : "Received"),
        h("span", { class: "subj" }, MESSAGE_KINDS[m.kind] || cap(m.kind)),
        h("span", { class: "who" }, out ? `to ${m.to || "?"}` : `from ${m.from || "?"}`),
        h("span", { class: "who" }, when),
      ),
      m.subject ? h("div", { class: "who" }, m.subject) : null,
      reading ? h("div", { class: "reading" }, h("b", {}, "The agent read it as: "), reading) : null,
      out && !m.sent_at && m.draft_ref ? h("div", { class: "who" }, `Draft saved: ${m.draft_ref}`) : null,
      m.body ? h("details", {}, h("summary", {}, "Read the email"), h("pre", {}, m.body)) : null,
    );
  });
  return h(
    "details",
    { class: "section" },
    h("summary", {}, `Emails (${d.messages.length})`),
    items.length ? items : empty("No emails yet."),
  );
}

function timelineSection(d) {
  return h(
    "details",
    { class: "section" },
    h("summary", {}, "History"),
    h(
      "ol",
      { class: "tl" },
      d.timeline.map((t) =>
        h(
          "li",
          { class: t.type },
          h("div", { class: "when" }, `${fmtInstant(t.at)}${t.actor ? ` · ${t.actor}` : ""}`),
          h("div", { class: "what" }, t.title),
          t.summary ? h("div", { class: "sum" }, t.summary) : null,
        ),
      ),
    ),
  );
}

// Who a change is recorded under: the server knows the signed-in person; without sign-in it is
// the name typed in the top bar (or the panel). Null when that is still empty.
function decisionBy() {
  if (state.viewer.signed_in) return {};
  const by = state.me.trim();
  if (!by) {
    toast("Type your name first (Your name); it is kept with every decision.", true);
    (drawer.hidden ? meInput : drawer.querySelector(".me-inline") || meInput).focus();
    return null;
  }
  return { by };
}

async function act(id, action, body, done) {
  const by = decisionBy();
  if (!by) return;
  try {
    const detail = await api(`/cases/${id}/${action}`, { method: "POST", body: JSON.stringify({ ...body, ...by }) });
    renderDrawer(detail);
    toast(done);
    renderTab();
  } catch (err) {
    toast(err.message, true);
  }
}

const approveCase = (d) => act(d.id, "approve", {}, "Approved: the pickup is booked");

// ------------------------------------------------------------------ access (admins)
//
// One row per person, one column per customer file. Changes are collected and saved together;
// the server records who made them (the signed-in admin) in the history below the grid.

const ACTIONS = { add: "added", grant: "gave", change: "changed", revoke: "took back", remove: "removed" };

async function renderAccess() {
  const [grid, changes] = await Promise.all([request("/api/access"), request("/api/access/history?limit=40")]);
  const pending = new Map(); // "email|customer" -> { email, customer, level, until }
  const names = Object.fromEntries(grid.customers.map((c) => [c.key, c.name]));
  const people = Object.fromEntries(grid.people.map((p) => [p.email, p.name || p.email]));
  const saveBtn = h("button", { class: "btn primary", type: "button" }, "Save changes");
  const discardBtn = h("button", { class: "btn", type: "button" }, "Discard");
  const pendingText = h("span", { class: "muted" });
  const showPending = () => {
    pendingText.textContent = pending.size ? plural(pending.size, "unsaved change") : "";
    saveBtn.hidden = discardBtn.hidden = !pending.size;
  };

  const cell = (person, customer) => {
    const held = person.access[customer.key];
    const level = held ? held.level : "none";
    const select = h(
      "select",
      { "aria-label": `${person.name || person.email}: ${customer.name}` },
      Object.entries(LEVELS).map(([value, label]) => h("option", { value }, label)),
    );
    select.value = level;
    const until = h("input", { type: "date", value: held && held.until ? held.until : "", "aria-label": "Until (optional)" });
    const untilLabel = h("label", { class: "until" }, "until", until);
    untilLabel.hidden = level === "none";
    const note = held
      ? h("div", { class: "sub" }, `${held.ended ? "Ended" : "From"} ${held.granted_by} · ${fmtInstant(held.granted_at).split(",")[0]}`)
      : null;
    const changed = () => {
      const key = `${person.email}|${customer.key}`;
      const now = { level: select.value, until: until.value || "" };
      const was = { level, until: (held && held.until) || "" };
      untilLabel.hidden = select.value === "none";
      if (now.level === was.level && (now.level === "none" || now.until === was.until)) pending.delete(key);
      else pending.set(key, { email: person.email, customer: customer.key, level: now.level, until: now.until || null });
      showPending();
    };
    select.addEventListener("change", changed);
    until.addEventListener("change", changed);
    return h("td", {}, select, untilLabel, note);
  };

  const removeBtn = (person) =>
    h(
      "button",
      {
        class: "btn small danger",
        type: "button",
        onclick: async () => {
          if (!confirm(`Take ${person.name || person.email} off the board? Every customer they see is taken back.`)) return;
          const by = decisionBy();
          if (!by) return;
          try {
            await request(`/api/access/people/${encodeURIComponent(person.email)}/remove`, { method: "POST", body: JSON.stringify(by) });
            toast(`${person.name || person.email} removed`);
            renderTab();
          } catch (err) {
            toast(err.message, true);
          }
        },
      },
      "Remove",
    );

  const head = h(
    "tr",
    {},
    h("th", { scope: "col" }, "Person"),
    grid.customers.map((c) => h("th", { scope: "col" }, c.name)),
    h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Remove")),
  );
  const adminRows = grid.admins.map((email) =>
    h(
      "tr",
      {},
      h("td", {}, h("div", { class: "strong" }, email), h("div", { class: "sub" }, "Admin (FP_BOARD_ADMINS)")),
      h("td", { colspan: String(grid.customers.length + 1) }, "Every customer, and gives access"),
    ),
  );
  const personRows = grid.people.map((p) =>
    h(
      "tr",
      {},
      h(
        "td",
        {},
        h("div", { class: "strong" }, p.name || p.email),
        h("div", { class: "sub" }, p.name ? p.email : ""),
        h("div", { class: "sub" }, p.last_seen_at ? `Last here ${ago(p.last_seen_at)}` : "Not signed in yet"),
      ),
      grid.customers.map((c) => cell(p, c)),
      h("td", { class: "nowrap" }, removeBtn(p)),
    ),
  );

  const email = h("input", { type: "email", required: true, placeholder: "name@circledelivers.com", autocomplete: "off", "aria-label": "Email" });
  const addForm = h(
    "form",
    { class: "add-person" },
    email,
    h("button", { class: "btn primary", type: "submit" }, "Add person"),
  );
  addForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const by = decisionBy();
    if (!by) return;
    try {
      await request("/api/access/people", { method: "POST", body: JSON.stringify({ email: email.value, ...by }) });
      toast(`${email.value.trim()} added; now choose their customers`);
      renderTab();
    } catch (err) {
      toast(err.message, true);
    }
  });

  saveBtn.addEventListener("click", async () => {
    const by = decisionBy();
    if (!by) return;
    try {
      await request("/api/access", { method: "POST", body: JSON.stringify({ changes: [...pending.values()], ...by }) });
      toast("Access saved");
      renderTab();
    } catch (err) {
      toast(err.message, true);
    }
  });
  discardBtn.addEventListener("click", () => renderTab());

  const line = (c) => {
    const who = people[c.email] || c.email;
    const what = c.customer_key ? `${names[c.customer_key] || c.customer_key}` : "";
    let text;
    if (c.action === "add") text = `added ${who} to the board`;
    else if (c.action === "remove") text = `removed ${who} from the board`;
    else if (c.action === "revoke") text = `took back ${who}'s access to ${what}`;
    else text = `${ACTIONS[c.action] || c.action} ${who} ${LEVELS[c.level] || c.level} on ${what}${c.until ? `, until ${fmtSlot(c.until)}` : ""}`;
    return h("li", {}, h("span", { class: "when" }, fmtInstant(c.at)), ` ${c.by} ${text}`);
  };

  view.replaceChildren(
    ...show(
      h("div", { class: "page-head" }, h("h1", {}, "Access")),
      h(
        "p",
        { class: "lead" },
        "Admins see every customer. Everyone else sees only the customers you give them here: View to follow the bookings, View and act to also approve, book and cancel.",
      ),
      state.viewer.signed_in
        ? null
        : h(
            "div",
            { class: "notice" },
            icon("info"),
            "Sign-in is off, so anyone who opens this board sees every customer. What you set here applies once Google sign-in is on.",
          ),
      grid.customers.length ? null : h("div", { class: "notice" }, "There are no customer files yet; add one with customers new."),
      addForm,
      h(
        "div",
        { class: "table-wrap" },
        h("table", { class: "access" }, h("thead", {}, head), h("tbody", {}, adminRows, personRows)),
      ),
      grid.people.length ? null : empty("Nobody else is on the board yet. Add someone by email above."),
      h("div", { class: "save-bar" }, pendingText, discardBtn, saveBtn),
      box("History", changes.length, changes.length ? h("ol", { class: "changes" }, changes.map(line)) : empty("No changes yet.")),
    ),
  );
  showPending();
}

// ------------------------------------------------------------------ start

// Who is signed in decides the top bar: their name and Sign out instead of the name box, the
// Access tab for admins, and a note for someone nobody has given a customer yet.
async function loadMe() {
  try {
    state.viewer = await request("/api/me");
  } catch (err) {
    view.replaceChildren(h("section", { class: "box" }, h("div", { class: "error" }, err.message)));
    return false;
  }
  const v = state.viewer;
  accessTab.hidden = !v.admin;
  meLabel.hidden = v.signed_in;
  whoEl.hidden = signoutForm.hidden = !v.signed_in;
  if (v.signed_in) whoEl.replaceChildren(v.name || v.email, v.admin ? h("span", { class: "role" }, " · admin") : "");
  if (v.signed_in && !v.admin && !v.customers.length) {
    const ask = v.admins.length ? ` Ask ${v.admins.join(" or ")} to give you your customers.` : "";
    view.replaceChildren(
      h(
        "section",
        { class: "box" },
        h("div", { class: "empty" }, `You're signed in as ${v.email}, but no customer has been given to you yet.${ask}`),
      ),
    );
    return false;
  }
  return true;
}

async function loadCustomers() {
  try {
    const names = await api("/customers");
    customerSelect.replaceChildren(
      h("option", { value: "" }, "All customers"),
      ...names.map((n) => h("option", { value: n }, n)), // spread: an array would print as text
    );
    customerSelect.value = names.includes(state.customer) ? state.customer : "";
    state.customer = customerSelect.value;
  } catch {
    /* the board still works across all customers */
  }
}

async function loadKinds() {
  try {
    [state.kinds, state.references] = await Promise.all([api("/kinds"), api("/references")]);
  } catch {
    state.kinds = [];
    state.references = [];
  }
}

meInput.value = state.me;
meInput.addEventListener("input", () => setMe(meInput.value));
customerSelect.addEventListener("change", () => {
  state.customer = customerSelect.value;
  save("fp.customer", state.customer);
  renderTab();
});
document.getElementById("refresh").addEventListener("click", async () => {
  await renderTab();
  if (state.caseId) await openCase(state.caseId);
});
scrim.addEventListener("click", () => closeDrawer());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !drawer.hidden) closeDrawer();
});
window.addEventListener("hashchange", route);
// Keep the board current while it sits open on a screen, but never under someone's hands.
setInterval(() => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (document.visibilityState === "visible" && drawer.hidden && !typing) renderTab();
}, 120000);

loadMe().then(async (ok) => {
  if (!ok) return;
  await Promise.all([loadCustomers(), loadKinds()]);
  await route();
});
