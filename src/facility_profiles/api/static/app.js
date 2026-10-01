// Pickup appointments board. Reads /api/booking and renders an overview, a filterable list, a
// week calendar and one case at a time in a side drawer. Every value from the server is placed
// with text nodes (vendor emails are untrusted text), never as HTML.

const API = "/api/booking";
const view = document.getElementById("view");
const drawer = document.getElementById("drawer");
const scrim = document.getElementById("scrim");
const toastEl = document.getElementById("toast");
const meInput = document.getElementById("me");
const customerSelect = document.getElementById("customer");
const stampEl = document.getElementById("stamp");

const STATUS = {
  unscheduled: "Unscheduled",
  pending: "Pending",
  scheduled: "Scheduled",
  declined: "Declined",
  canceled: "Canceled",
};
const STEP_NAMES = { unscheduled: "Not requested", pending: "Requested", scheduled: "Booked" };
const MESSAGE_KINDS = {
  request: "Request",
  reschedule: "Reschedule request",
  follow_up: "Follow-up",
  acknowledge: "Thank you",
  accept_offer: "Accepted the offer",
  ask_alternative: "Asked for other days",
  answer_question: "Answer",
  escalate_to_customer: "Note to the customer desk",
  notify_customer_desk: "Note to the customer desk",
  reply: "Vendor reply",
  customer_desk: "Customer desk",
};
const READING_TONE = {
  confirmed: "status-scheduled",
  counter_offer: "todo",
  question: "todo",
  rejected: "status-declined",
  deferred: "status-pending",
};
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const TABS = ["overview", "appointments", "week"];

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

const pad = (n) => String(n).padStart(2, "0");
const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const pos = (row) => (row.po_numbers && row.po_numbers.length ? row.po_numbers.join(", ") : "-");

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// A pickup slot is vendor-local wall time written "YYYY-MM-DD" or "YYYY-MM-DD HH:MM"; it is
// shown as written, with its weekday, never converted.
function parseLocal(local) {
  if (!local) return null;
  const [day, time] = local.split(" ");
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return null;
  return { day, y, m, d, time: time || null, weekday: WEEKDAYS[new Date(y, m - 1, d).getDay()] };
}

function fmtSlot(local) {
  const p = parseLocal(local);
  if (!p) return "No date";
  return `${p.weekday} ${pad(p.m)}/${pad(p.d)}${p.time ? ` ${p.time}` : ""}`;
}

// Instants (sent, raised, delivery) are UTC on the wire and shown in the reader's own time.
function fmtInstant(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ago(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function startOfToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
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
  }, 3600);
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}

function query(params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined && value !== false) q.set(key, value);
  }
  const text = q.toString();
  return text ? `?${text}` : "";
}

// ------------------------------------------------------------------ state and routing

const defaultFilters = () => ({ status: "", exception: "", q: "", start: "", end: "", pastDue: false });

const state = {
  tab: "overview",
  renderedTab: null,
  caseId: null,
  customer: load("fp.customer", ""),
  me: load("fp.me", ""),
  filters: defaultFilters(),
  weekStart: mondayOf(startOfToday()),
  kinds: [],
  refocus: null,
};

function parseHash() {
  const [tab, id] = location.hash.replace(/^#/, "").split("/");
  return { tab: TABS.includes(tab) ? tab : "overview", caseId: id ? Number(id) : null };
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
  if (state.tab === "appointments") renderTab();
  else go("appointments");
}

function setFilter(patch, refocus = null) {
  Object.assign(state.filters, patch);
  state.refocus = refocus;
  renderTab();
}

async function renderTab() {
  state.renderedTab = state.tab;
  try {
    if (state.tab === "overview") await renderOverview();
    else if (state.tab === "appointments") await renderAppointments();
    else await renderWeek();
    const now = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    stampEl.textContent = `Updated ${now}`;
  } catch (err) {
    view.replaceChildren(
      h(
        "section",
        { class: "panel" },
        h("div", { class: "error" }, `Could not load the board: ${err.message}`),
        h("div", { class: "empty" }, h("button", { class: "btn", type: "button", onclick: renderTab }, "Try again")),
      ),
    );
  }
}

// ------------------------------------------------------------------ shared pieces

const statusChip = (status) => h("span", { class: `chip status-${status}` }, STATUS[status] || status);

function flags(row) {
  return h(
    "span",
    { class: "chips" },
    row.past_due ? h("span", { class: "chip late" }, "Past due") : null,
    row.draft_ready ? h("span", { class: "chip draft" }, "Draft ready") : null,
    row.open_exceptions.map((e) => h("span", { class: "chip todo", title: cap(e.description) }, e.label)),
  );
}

function panel(title, count, children) {
  return h(
    "section",
    { class: "panel" },
    h("header", {}, h("h2", {}, title), h("span", { class: "count" }, count)),
    h("div", { class: "body" }, children),
  );
}

const empty = (text) => h("div", { class: "empty" }, text);

function caseRow(row) {
  return h(
    "button",
    { class: "row", type: "button", onclick: () => go(state.tab, row.id) },
    h("span", { class: "title" }, row.vendor || "Unknown vendor"),
    statusChip(row.status),
    h("span", { class: "meta" }, `${fmtSlot(row.pickup_local)} · PO ${pos(row)} · ${row.stage}`),
  );
}

// ------------------------------------------------------------------ overview

async function renderOverview() {
  const data = await api(`/overview${query({ customer: state.customer })}`);
  const c = data.counts;
  const tile = (key, label, sub, filter, tone) =>
    h(
      "button",
      { class: `tile${tone ? ` ${tone}` : ""}`, type: "button", onclick: () => showList(filter) },
      h("div", { class: "label" }, label),
      h("div", { class: "value" }, c[key]),
      h("div", { class: "sub" }, sub),
    );
  view.replaceChildren(
    h("h1", {}, "Overview"),
    h(
      "section",
      { class: "tiles", "aria-label": "Totals" },
      tile("needs_action", "Needs action", "Waiting for a person", { exception: "any" }, c.needs_action ? "warn" : ""),
      tile("past_due", "Past due", "Pickup passed, not booked", { pastDue: true }, c.past_due ? "bad" : ""),
      tile(
        "not_requested",
        "Not requested",
        `${plural(c.drafts_waiting, "draft")} waiting to be sent`,
        { status: "unscheduled" },
        "",
      ),
      tile("waiting_on_vendor", "Waiting on vendor", "Request out, no answer yet", { status: "pending" }, ""),
      tile(
        "booked_upcoming",
        `Booked, next ${data.days} days`,
        `${plural(c.booked, "pickup")} booked in all`,
        { status: "scheduled" },
        "good",
      ),
    ),
    h(
      "div",
      { class: "grid2" },
      panel(
        "Needs action",
        String(data.todos.length),
        data.todos.length ? data.todos.map(todoRow) : empty("Nothing needs a person right now."),
      ),
      panel(
        `Coming up, next ${data.days} days`,
        String(data.upcoming.length),
        data.upcoming.length ? byDay(data.upcoming) : empty("No pickups in the next days."),
      ),
    ),
    h(
      "div",
      { class: "grid2" },
      panel(
        "Past due, not booked",
        String(data.past_due.length),
        data.past_due.length ? data.past_due.map(caseRow) : empty("Nothing has slipped."),
      ),
      panel("To-dos by type", "", data.todos_by_kind.length ? bars(data.todos_by_kind) : empty("No open to-dos.")),
    ),
  );
}

function todoRow(todo) {
  const c = todo.case;
  return h(
    "button",
    { class: "row", type: "button", onclick: () => go(state.tab, c.id) },
    h("span", { class: "title" }, h("span", { class: "chip todo" }, todo.label), " ", c.vendor || "Unknown vendor"),
    h("span", { class: "muted" }, fmtSlot(c.pickup_local)),
    h("span", { class: "meta" }, `${cap(todo.description)} · PO ${pos(c)} · raised ${ago(todo.raised_at)}`),
  );
}

function byDay(rows) {
  const out = [];
  let last = null;
  for (const row of rows) {
    if (row.pickup_date !== last) {
      last = row.pickup_date;
      out.push(h("div", { class: "day-head" }, fmtSlot(row.pickup_date)));
    }
    const todos = row.open_exceptions.length ? ` · ${plural(row.open_exceptions.length, "to-do")}` : "";
    out.push(
      h(
        "button",
        { class: "row", type: "button", onclick: () => go(state.tab, row.id) },
        h("span", { class: "title" }, `${parseLocal(row.pickup_local)?.time || "Any time"} · ${row.vendor || "Unknown vendor"}`),
        statusChip(row.status),
        h("span", { class: "meta" }, `PO ${pos(row)} · ${row.stage}${todos}`),
      ),
    );
  }
  return out;
}

function bars(items) {
  const max = Math.max(...items.map((i) => i.count));
  return h(
    "div",
    { class: "bars" },
    items.map((item) =>
      h(
        "button",
        { class: "bar", type: "button", onclick: () => showList({ exception: item.kind }) },
        h("span", {}, item.label),
        h(
          "span",
          { class: "track", "aria-hidden": "true" },
          h("span", { class: "fill", style: `width:${Math.max(6, Math.round((100 * item.count) / max))}%` }),
        ),
        h("span", { class: "n" }, item.count),
      ),
    ),
  );
}

// ------------------------------------------------------------------ appointments list

async function renderAppointments() {
  const f = state.filters;
  const rows = await api(
    `/cases${query({
      customer: state.customer,
      status: f.status,
      exception: f.exception,
      q: f.q,
      start: f.start,
      end: f.end,
      past_due: f.pastDue ? "true" : "",
    })}`,
  );
  const statusButtons = h(
    "div",
    { class: "seg", role: "group", "aria-label": "Status" },
    [["", "All"], ...Object.entries(STATUS)].map(([value, label]) =>
      h(
        "button",
        { type: "button", "aria-pressed": String(f.status === value), onclick: () => setFilter({ status: value }) },
        label,
      ),
    ),
  );
  const kindSelect = h(
    "select",
    { "aria-label": "To-do", onchange: (e) => setFilter({ exception: e.target.value }) },
    h("option", { value: "" }, "Any to-do or none"),
    h("option", { value: "any" }, "Needs action (any to-do)"),
    state.kinds.map((k) => h("option", { value: k.kind }, k.label)),
  );
  kindSelect.value = f.exception;
  const search = h("input", {
    type: "search",
    placeholder: "Search PO, load, vendor, pickup #",
    "aria-label": "Search",
    value: f.q,
  });
  search.addEventListener(
    "input",
    debounce(() => setFilter({ q: search.value }, "search"), 300),
  );
  const from = h("input", {
    type: "date",
    "aria-label": "Pickup from",
    value: f.start,
    onchange: (e) => setFilter({ start: e.target.value }),
  });
  const to = h("input", {
    type: "date",
    "aria-label": "Pickup to",
    value: f.end,
    onchange: (e) => setFilter({ end: e.target.value }),
  });
  const late = h(
    "label",
    { class: "toggle" },
    h("input", { type: "checkbox", checked: f.pastDue, onchange: (e) => setFilter({ pastDue: e.target.checked }) }),
    "Past due only",
  );
  const headers = ["Pickup", "Vendor", "Customer", "PO", "Load", "Status", "Needs", "Pickup #", "Delivery", "Activity"];
  const table = rows.length
    ? h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, headers.map((t) => h("th", { scope: "col" }, t)))),
          h("tbody", {}, rows.map(tableRow)),
        ),
      )
    : h("section", { class: "panel" }, empty("No appointments match these filters."));
  view.replaceChildren(
    h("h1", {}, "Appointments"),
    h(
      "div",
      { class: "filters" },
      statusButtons,
      kindSelect,
      h("label", { class: "toggle" }, "From", from),
      h("label", { class: "toggle" }, "to", to),
      late,
      h("div", { class: "grow" }, search),
      h("button", { class: "btn small", type: "button", onclick: () => setFilter(defaultFilters()) }, "Clear"),
    ),
    h("p", { class: "result-count" }, plural(rows.length, "appointment")),
    table,
  );
  if (state.refocus === "search") {
    search.focus();
    search.setSelectionRange(search.value.length, search.value.length);
  }
  state.refocus = null;
}

function tableRow(row) {
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
      { class: "nowrap mono" },
      h("div", { class: row.past_due ? "late-text" : "" }, fmtSlot(row.pickup_local)),
      h("div", { class: "sub" }, row.pickup_source),
    ),
    h("td", {}, h("div", {}, row.vendor || "Unknown vendor"), h("div", { class: "sub" }, row.vendor_city || "")),
    h("td", {}, row.customer || ""),
    h("td", { class: "mono" }, pos(row)),
    h("td", { class: "mono" }, row.load_id),
    h("td", {}, statusChip(row.status), h("div", { class: "sub" }, row.stage)),
    h("td", {}, flags(row)),
    h("td", { class: "mono" }, row.pickup_number || ""),
    h(
      "td",
      {},
      h("div", {}, row.delivery_site || ""),
      h("div", { class: "sub" }, [row.delivery_ref, fmtInstant(row.delivery_at)].filter(Boolean).join(" · ")),
    ),
    h("td", { class: "nowrap sub" }, ago(row.last_activity)),
  );
}

// ------------------------------------------------------------------ week

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
      h("h1", {}, `Week of ${fmtSlot(isoDay(start))}`),
      h("button", { class: "btn small", type: "button", onclick: () => shift(-7) }, "Previous"),
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
      h("button", { class: "btn small", type: "button", onclick: () => shift(7) }, "Next"),
      h("span", { class: "muted" }, plural(rows.length, "pickup")),
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
          h("header", {}, fmtSlot(key), h("span", { class: "n" }, items.length ? String(items.length) : "")),
          h("div", { class: "items" }, items.length ? items.map(card) : h("div", { class: "none" }, "No pickups")),
        );
      }),
    ),
  );
}

function card(row) {
  const todos = row.open_exceptions.length;
  return h(
    "button",
    { class: `card status-${row.status}`, type: "button", title: row.stage, onclick: () => go("week", row.id) },
    h(
      "div",
      { class: "t" },
      parseLocal(row.pickup_local)?.time || "Any time",
      todos ? h("span", { class: "flag" }, ` · ${plural(todos, "to-do")}`) : null,
    ),
    h("div", { class: "v" }, row.vendor || "Unknown vendor"),
    h("div", { class: "s" }, `PO ${pos(row)}`),
    h("div", { class: "s" }, row.past_due ? "Past due, not booked" : STATUS[row.status]),
  );
}

// ------------------------------------------------------------------ one case

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
      h("div", { class: "head" }, h("div", { class: "top" }, h("h2", { id: "case-title" }, `Case ${id}`), closeButton())),
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
    h("div", { class: "top" }, h("h2", { id: "case-title", tabindex: "-1" }, d.vendor || "Unknown vendor"), statusChip(d.status), closeButton()),
    h("div", { class: "ids" }, `PO ${pos(d)} · Load ${d.load_id} · ${d.customer || "No customer"} · Case #${d.id}`),
    steps(d.status),
    h("div", { class: "stage" }, d.stage, d.past_due ? h("span", { class: "chip late" }, "Past due") : null),
  );
  drawer.replaceChildren(
    head,
    h("div", { class: "content" }, todoSection(d), actionSection(d), factsSection(d), messagesSection(d), timelineSection(d)),
  );
  if (!sameCase) drawer.scrollTop = 0;
  drawer.querySelector("#case-title").focus({ preventScroll: sameCase });
}

// The name recorded with each decision. It is asked for in the top bar and again inside the
// case drawer, which covers the top bar while it is open; both fields stay in step.
function setMe(value) {
  state.me = value;
  save("fp.me", value);
  for (const input of [meInput, ...document.querySelectorAll(".me-inline")]) {
    if (input.value !== value) input.value = value;
  }
}

function nameField() {
  const input = h("input", {
    class: "me-inline",
    type: "text",
    value: state.me,
    placeholder: "your name",
    autocomplete: "name",
    size: "11",
    "aria-label": "Your name, recorded with the decision",
  });
  input.addEventListener("input", () => setMe(input.value));
  return h("label", { class: "toggle me-field" }, "Recorded as", input);
}

function steps(status) {
  const order = ["unscheduled", "pending", "scheduled"];
  const at = status === "declined" ? 1 : order.indexOf(status);
  const items = [];
  order.forEach((name, i) => {
    if (i) items.push(h("span", { class: "step-line", "aria-hidden": "true" }));
    const done = status === "scheduled" ? true : at > i;
    const now = status !== "scheduled" && at === i;
    items.push(
      h(
        "span",
        { class: `step${done ? " done" : ""}${now ? " now" : ""}` },
        h("span", { class: "dot", "aria-hidden": "true" }, done ? "✓" : String(i + 1)),
        STEP_NAMES[name],
      ),
    );
  });
  if (status === "declined" || status === "canceled") items.push(statusChip(status));
  return h("div", { class: "steps", "aria-label": `Progress: ${STATUS[status] || status}` }, items);
}

function todoSection(d) {
  const open = d.exceptions.filter((e) => e.open);
  if (!open.length) {
    return h("div", { class: "section" }, h("h3", {}, "Needs action"), empty("Nothing to do on this case."));
  }
  return h(
    "div",
    { class: "section" },
    h("h3", {}, `Needs action (${open.length})`),
    h("div", { class: "todos" }, open.map((e) => todoCard(d, e))),
  );
}

function todoCard(d, e) {
  const box = h(
    "div",
    { class: "todo-card" },
    h("div", { class: "k" }, e.label),
    h("div", { class: "d" }, cap(e.description)),
    e.hint ? h("div", { class: "hint" }, e.hint) : null,
    h("div", { class: "by" }, `Raised by ${e.raised_by} · ${fmtInstant(e.raised_at)} (${ago(e.raised_at)})`),
  );
  const acts = h("div", { class: "acts" });
  if (e.kind === "confirmation_review" && d.can_approve) {
    acts.append(h("button", { class: "btn primary small", type: "button", onclick: () => approveCase(d) }, "Approve slot"));
  }
  acts.append(h("button", { class: "btn small", type: "button", onclick: () => toggleForm(box, resolveForm(d, e)) }, "Resolve…"));
  box.append(acts);
  return box;
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

function formButtons(form, label) {
  return h(
    "div",
    { class: "acts" },
    h("button", { class: "btn small", type: "button", onclick: () => form.remove() }, "Back"),
    h("button", { class: "btn primary small", type: "submit" }, label),
  );
}

function resolveForm(d, e) {
  const note = h("textarea", { required: true, placeholder: "What was done, for example: answered the vendor by phone" });
  const form = h("form", { class: "form" }, h("label", { class: "full" }, "How was it resolved?", note));
  form.dataset.kind = `resolve-${e.kind}`;
  form.append(formButtons(form, "Resolve"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(d.id, "resolve", { kind: e.kind, note: note.value }, `${e.label}: resolved`);
  });
  return form;
}

function bookedForm(d) {
  const slot = parseLocal(d.confirmed_local || d.requested_local);
  const via = h("select", {}, ["phone", "portal", "email", "other"].map((v) => h("option", { value: v }, v)));
  const date = h("input", { type: "date", value: slot ? slot.day : "" });
  const time = h("input", { type: "time", value: slot && slot.time ? slot.time : "" });
  const number = h("input", { type: "text", value: d.pickup_number || "", placeholder: "for example 20463798" });
  const note = h("input", { type: "text", placeholder: "optional" });
  const form = h(
    "form",
    { class: "form" },
    h(
      "div",
      { class: "fields" },
      h("label", {}, "Booked by", via),
      h("label", {}, "Vendor pickup number", number),
      h("label", {}, "Pickup date", date),
      h("label", {}, "Pickup time (vendor's local time)", time),
      h("label", { class: "full" }, "Note", note),
    ),
  );
  form.dataset.kind = "booked";
  form.append(formButtons(form, "Mark booked"));
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
      },
      "Marked booked: the pickup is scheduled",
    );
  });
  return form;
}

function cancelForm(d) {
  const reason = h("textarea", { required: true, placeholder: "Why, for example: load canceled by the customer" });
  const form = h("form", { class: "form" }, h("label", { class: "full" }, "Why is this appointment not needed?", reason));
  form.dataset.kind = "cancel";
  form.append(formButtons(form, "Cancel appointment"));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(d.id, "cancel", { reason: reason.value }, "Appointment canceled");
  });
  return form;
}

function actionSection(d) {
  const section = h("div", { class: "section" }, h("h3", {}, "Actions"));
  const canceled = d.status === "canceled";
  section.append(
    h(
      "div",
      { class: "actionbar" },
      d.can_approve
        ? h("button", { class: "btn primary", type: "button", onclick: () => approveCase(d) }, "Approve confirmed slot")
        : null,
      h(
        "button",
        { class: "btn", type: "button", disabled: canceled, onclick: () => toggleForm(section, bookedForm(d)) },
        d.status === "scheduled" ? "Update booking…" : "Mark booked…",
      ),
      h(
        "button",
        { class: "btn danger", type: "button", disabled: canceled, onclick: () => toggleForm(section, cancelForm(d)) },
        "Cancel appointment…",
      ),
      nameField(),
    ),
  );
  return section;
}

function factsSection(d) {
  const delivery = [d.delivery_site, d.delivery_ref, d.delivery_at ? `${fmtInstant(d.delivery_at)} your time` : null]
    .filter(Boolean)
    .join(" · ");
  const desk = d.desk ? `${d.desk}${d.method ? ` (${d.method})` : ""}` : d.method || "None on the profile";
  const rows = [
    ["Pickup", `${fmtSlot(d.pickup_local)} (${d.pickup_source}, vendor's time${d.timezone ? `, ${d.timezone}` : ""})`],
    ["Requested", d.requested_local ? fmtSlot(d.requested_local) : "-"],
    ["Confirmed", d.confirmed_local ? fmtSlot(d.confirmed_local) : "-"],
    ["Pickup number", d.pickup_number || "-"],
    ["Vendor", [d.vendor, d.vendor_city].filter(Boolean).join(", ") || "-"],
    ["Booking desk", desk],
    ["Delivery", delivery || "-"],
    ["Tendered pickup", d.tendered_pickup_at ? `${fmtInstant(d.tendered_pickup_at)} your time` : "-"],
    ["Miles", d.miles ?? "-"],
    ["Reschedules", d.reschedule_count],
    ["Customer", d.customer || "-"],
  ];
  return h(
    "div",
    { class: "section" },
    h("h3", {}, "Appointment"),
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
    if (!out && r.skipped) reading = h("span", { class: "chip" }, `Not read as an answer: ${r.skipped}`);
    else if (!out && r.status) {
      const bits = [r.pickup_date, r.pickup_time, r.pickup_number ? `pickup# ${r.pickup_number}` : null].filter(Boolean);
      reading = h("span", { class: `chip ${READING_TONE[r.status] || ""}` }, `Read as ${r.status.replace("_", " ")}${bits.length ? ` · ${bits.join(" ")}` : ""}`);
    }
    return h(
      "div",
      { class: "msg" },
      h(
        "div",
        { class: "line" },
        h("span", { class: `dir ${out ? "out" : "in"}`, "aria-label": out ? "sent" : "received" }, out ? "→" : "←"),
        h("span", { class: "subj" }, MESSAGE_KINDS[m.kind] || m.kind),
        h("span", { class: "who" }, out ? `to ${m.to || "?"}` : `from ${m.from || "?"}`),
        h("span", { class: "who" }, when),
      ),
      m.subject ? h("div", { class: "who" }, m.subject) : null,
      reading ? h("div", { class: "reading" }, reading) : null,
      out && !m.sent_at && m.draft_ref ? h("div", { class: "who" }, `Draft saved: ${m.draft_ref}`) : null,
      m.body ? h("details", {}, h("summary", {}, "Show email"), h("pre", {}, m.body)) : null,
    );
  });
  return h(
    "div",
    { class: "section" },
    h("h3", {}, `Emails (${d.messages.length})`),
    items.length ? items : empty("No emails yet."),
  );
}

function timelineSection(d) {
  return h(
    "div",
    { class: "section" },
    h("h3", {}, "Timeline"),
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

async function act(id, action, body, done) {
  const by = state.me.trim();
  if (!by) {
    toast("Enter your name first (Recorded as); it is kept with every decision.", true);
    (drawer.querySelector(".me-inline") || meInput).focus();
    return;
  }
  try {
    const detail = await api(`/cases/${id}/${action}`, { method: "POST", body: JSON.stringify({ ...body, by }) });
    renderDrawer(detail);
    toast(done);
    renderTab();
  } catch (err) {
    toast(err.message, true);
  }
}

const approveCase = (d) => act(d.id, "approve", {}, "Approved: the pickup is scheduled");

// ------------------------------------------------------------------ start

async function loadCustomers() {
  try {
    const names = await api("/customers");
    customerSelect.replaceChildren(
      h("option", { value: "" }, "All customers"),
      names.map((n) => h("option", { value: n }, n)),
    );
    customerSelect.value = names.includes(state.customer) ? state.customer : "";
    state.customer = customerSelect.value;
  } catch {
    /* the board still works across all customers */
  }
}

async function loadKinds() {
  try {
    state.kinds = await api("/kinds");
  } catch {
    state.kinds = [];
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

Promise.all([loadCustomers(), loadKinds()]).then(route);
