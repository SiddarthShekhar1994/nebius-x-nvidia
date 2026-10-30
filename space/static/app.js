"use strict";
// Keeper's live demo page. Everything from the server or a model is shown with textContent,
// never as HTML, because email text and model output are untrusted.

const PRESETS = [
  {label: "An offer from Example Inc", sender: "Jordan Lee <jordan.lee@careers.example.test>",
   subject: "Your Software Engineer Intern offer",
   body: "Example Inc is pleased to offer you the Software Engineer Intern position for summer 2027. Please reply to let me know you received this letter."},
  {label: "A question from the recruiter", sender: "Jordan Lee <jordan.lee@careers.example.test>",
   subject: "Before your Software Engineer Intern interview",
   body: "Hi, this is Jordan from Example Inc. Before your Software Engineer Intern interview, could you tell me which project you would like to walk us through?"},
  {label: "A too-good-to-be-true job", sender: "Campus Jobs Desk <campus.jobs.desk@gmail.com>",
   subject: "Remote internship, $200/hr, no interview",
   body: "You were selected for a paid remote internship. Pay the $50 registration fee today to hold your spot."},
  {label: "An interview at a company you don't track", sender: "Priya Shah <priya.shah@acme-robotics.example.test>",
   subject: "Interview invitation: Data Analyst Intern",
   body: "Acme Robotics would like to interview you for Data Analyst Intern. Please reply with two times that work for you next week."},
  {label: "A club newsletter", sender: "Campus ACM Chapter <news@acm-chapter.example.test>",
   subject: "This week: resume workshop",
   body: "Our resume workshop is Thursday at 6 pm in the engineering building. Bring a laptop and a draft resume."},
  {label: "Write your own", sender: "", subject: "", body: ""},
];
const BUCKETS = {junk: "Junk", actionable: "Needs action", due: "Has a deadline", newsletter: "Newsletter"};
const STEPS = {tracker: "Tracker", deadlines: "Calendar", drafts: "Reply draft"};
const STATUSES = {done: "Done", already_done: "Already done", planned: "Planned", skipped: "Skipped",
                  review: "Needs you", failed: "Failed"};
const APP_STATUS = {found: "Found", applied: "Applied", interview: "Interview", decision: "Decision"};
const FLAGS = {recruiter_identity_unverified: "Keeper can't tell who sent it",
               application_context_missing: "it doesn't match an application you track",
               sensitive_details: "it asks for sensitive details"};
const ERRORS = {ProviderUnavailable: "Nemotron didn't answer in time. Run the step again.",
                RateLimited: "Token Factory asked the demo to slow down. Try again in a minute."};
const INSTANT = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z/g;
const EMPTY = {tracker: [], calendar: [], drafts: [], usage: [], done: [], cards: [], calls: 0};
const KEY = "keeper-demo-session";

const $ = (id) => document.getElementById(id);
let session = storedSession();
let config = {live: false};
let lastState = EMPTY;
let busy = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function newSessionId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function remember(id) {
  try { sessionStorage.setItem(KEY, id); } catch (error) { /* storage blocked: the visit won't survive a reload */ }
  return id;
}

function storedSession() {
  try {
    const value = sessionStorage.getItem(KEY);
    if (value && /^[0-9a-f]{32}$/.test(value)) return value;
  } catch (error) { /* fall through to a fresh visit */ }
  return remember(newSessionId());
}

async function api(path, body) {
  const options = body === undefined ? {cache: "no-store"}
    : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (error) { /* not JSON */ }
  if (!response.ok) throw new Error(data.error || `The server answered ${response.status}.`);
  return data;
}

function parse(iso) {
  return new Date(String(iso).replace(/(\.\d{3})\d+/, "$1"));
}

function when(iso) {
  const date = parse(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString("en-US", {timeZone: "UTC", weekday: "short", month: "short", day: "numeric",
                                       hour: "numeric", minute: "2-digit"}) + " UTC";
}

function day(iso) {
  const date = parse(iso);
  return Number.isNaN(date.getTime()) ? String(iso)
    : date.toLocaleDateString("en-US", {timeZone: "UTC", month: "short", day: "numeric"});
}

function friendly(text) {
  return String(text).replace(INSTANT, (match) => when(match));
}

function words(identifier) {
  return String(identifier).replace(/_/g, " ");
}

function showNotice(message, bad) {
  const notice = $("notice");
  notice.textContent = message;
  notice.classList.toggle("bad", Boolean(bad));
  notice.hidden = !message;
}

function draftView(draft) {
  const box = el("details", "draft");
  box.open = true;
  const flags = draft.flags || [];
  const who = flags.length
    ? `Holding reply, because ${flags.map((flag) => FLAGS[flag] || words(flag)).join(" and ")}. Saved for you to review, never sent.`
    : "Reply written by Nemotron 3 Super. Saved as a draft, never sent.";
  box.append(el("summary", null, who), el("pre", null, draft.body));
  return box;
}

function cardView(card, pending) {
  const node = el("article", pending ? "card pending" : "card");
  node.dataset.id = card.email.id;
  const head = el("div", "card-head");
  head.append(el("span", null, card.email.sender), el("span", null, when(card.email.received_at)));
  node.append(head, el("h3", null, card.email.subject), el("p", "body", card.email.body));
  if (pending) {
    node.append(el("p", "working", "Nemotron 3 Nano is reading it, then Keeper acts on what it decided"));
    return node;
  }
  if (card.error) {
    node.append(el("p", "error", ERRORS[card.error] || `Keeper couldn't finish this email (${card.error}).`));
    return node;
  }
  const triage = card.triage;
  const chips = el("div", "chips");
  chips.append(el("span", `chip ${triage.bucket || "review"}`, BUCKETS[triage.bucket] || "Needs review"));
  if (triage.career) chips.append(el("span", "chip", "Career"));
  if (triage.extracted_deadline) chips.append(el("span", "chip deadline", "Deadline " + when(triage.extracted_deadline)));
  if (triage.scam_signals.length) {
    chips.append(el("span", "chip warn", "Scam warning: " + triage.scam_signals.map(words).join(", ")));
  }
  const reason = el("p", "reason");
  reason.append(el("strong", null, "Nemotron 3 Nano: "), document.createTextNode(triage.reason));
  node.append(chips, reason);
  if (card.labels.length) node.append(el("p", "labels", "Gmail labels added: " + card.labels.join(", ")));
  const list = el("ul", "outcomes");
  for (const outcome of card.outcomes) {
    const item = el("li", `outcome ${outcome.status}`);
    item.append(el("span", "step", STEPS[outcome.step] || outcome.step),
                el("span", "status", STATUSES[outcome.status] || outcome.status),
                el("span", "detail", friendly(outcome.detail)));
    list.append(item);
  }
  if (!card.outcomes.length) {
    const item = el("li", "outcome skipped");
    item.append(el("span", "step", "Actions"), el("span", "status", "None"),
                el("span", "detail", triage.scam_signals.length
                  ? "Keeper doesn't act on mail with scam warnings: no tracker change, no event, no draft."
                  : "Nothing here for Keeper to do."));
    list.append(item);
  }
  node.append(list);
  for (const draft of card.drafts) node.append(draftView(draft));
  return node;
}

function item(title, detail) {
  const box = el("div", "item");
  box.append(el("p", "event-name", title));
  if (detail) box.append(el("p", "muted small", detail));
  return box;
}

function renderTracker(apps) {
  const box = $("tracker");
  box.replaceChildren();
  if (!apps.length) {
    box.append(el("p", "muted small", "Nothing tracked yet. Step 1 starts tracking the internship."));
    return;
  }
  for (const app of apps) {
    const entry = el("div", "item");
    const status = app.status === "decision" && app.outcome ? `Decision: ${app.outcome}` : (APP_STATUS[app.status] || app.status);
    entry.append(el("p", "app-name", `${app.organization} · ${app.title}`), el("span", `chip ${app.status}`, status));
    const history = el("ol", "history");
    for (const change of app.history) {
      history.append(el("li", null, `${APP_STATUS[change.status] || change.status}, ${day(change.at)}`));
    }
    entry.append(history);
    box.append(entry);
  }
}

function renderCalendar(events) {
  const box = $("calendar");
  box.replaceChildren();
  if (!events.length) {
    box.append(el("p", "muted small", "No events yet. Keeper adds one when an email gives an explicit deadline for something you track."));
    return;
  }
  for (const event of events) box.append(item(event.summary, when(event.at)));
}

function renderDrafts(drafts) {
  const box = $("drafts");
  box.replaceChildren();
  if (!drafts.length) {
    box.append(el("p", "muted small", "No drafts yet. Keeper saves replies as drafts and never sends them."));
    return;
  }
  for (const draft of drafts) {
    box.append(item(draft.subject.replace("[SYNTHETIC] ", ""),
                    draft.flags.length ? "Holding reply for you to review" : "Written by Nemotron 3 Super"));
  }
}

function renderUsage(usage) {
  const box = $("usage");
  box.replaceChildren();
  if (!usage.length) {
    box.append(el("p", "muted small", "No model calls yet. Each one goes through Keeper's router to Nebius Token Factory."));
    return;
  }
  const table = el("table");
  const head = el("tr");
  head.append(el("th", null, "Model"), el("th", "num", "Calls"), el("th", "num", "Tokens"), el("th", "num", "Avg time"));
  table.append(head);
  for (const row of usage) {
    const line = el("tr");
    line.append(el("td", null, String(row.model).split("/").pop()), el("td", "num", String(row.calls)),
                el("td", "num", Number(row.tokens).toLocaleString("en-US")),
                el("td", "num", `${(Number(row.latency_ms) / 1000).toFixed(1)} s`));
    table.append(line);
  }
  box.append(table);
}

function renderSteps() {
  const done = lastState.done || [];
  const has = (step) => done.includes(step);
  $("track").disabled = busy || has("track");
  $("part1").disabled = busy || !has("track") || has("part1") || !config.live;
  $("part2").disabled = busy || !has("part1") || has("part2") || !config.live;
  $("send").disabled = busy || !config.live;
  for (const step of ["track", "part1", "part2"]) $(`step-${step}`).classList.toggle("done", has(step));
}

function render(state) {
  lastState = {...EMPTY, ...state};
  const cards = $("cards");
  cards.replaceChildren(...lastState.cards.slice().reverse().map((card) => cardView(card, false)));
  $("inbox-empty").hidden = lastState.cards.length > 0;
  renderTracker(lastState.tracker);
  renderCalendar(lastState.calendar);
  renderDrafts(lastState.drafts);
  renderUsage(lastState.usage);
  renderSteps();
}

function setBusy(value) {
  busy = value;
  renderSteps();
}

function onEvent(event) {
  const cards = $("cards");
  if (event.kind === "arrived") {
    $("inbox-empty").hidden = true;
    cards.prepend(cardView({email: event.email}, true));
  } else if (event.kind === "card") {
    const fresh = cardView(event.card, false);
    const pending = Array.from(cards.children).find((node) => node.dataset.id === event.card.email.id && node.classList.contains("pending"));
    if (pending) pending.replaceWith(fresh); else cards.prepend(fresh);
  } else if (event.kind === "tracked") {
    const app = event.application;
    showNotice(`Keeper is tracking ${app.organization}, ${app.title}, as found on ${day(app.updated_at)}.`);
  }
}

async function follow(job) {
  let since = 0;
  for (;;) {
    const data = await api(`api/job?id=${encodeURIComponent(job)}&since=${since}`);
    for (const event of data.events) onEvent(event);
    since = data.next;
    if (data.done) {
      if (data.state) render(data.state);
      if (data.error) showNotice(data.error, true);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

async function run(action, extra) {
  if (busy) return;
  setBusy(true);
  showNotice("");
  try {
    const {job} = await api("api/run", {session, action, ...extra});
    await follow(job);
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(false);
  }
}

function fillPreset(index) {
  const preset = PRESETS[index] || PRESETS[0];
  $("sender").value = preset.sender;
  $("subject").value = preset.subject;
  $("body").value = preset.body;
}

async function startOver() {
  if (busy) return;
  try { await api("api/reset", {session}); } catch (error) { /* a fresh visit works either way */ }
  session = remember(newSessionId());
  showNotice("");
  render(EMPTY);
}

async function init() {
  PRESETS.forEach((preset, index) => $("preset").append(new Option(preset.label, String(index))));
  fillPreset(0);
  $("preset").addEventListener("change", (event) => fillPreset(Number(event.target.value)));
  for (const action of ["track", "part1", "part2"]) $(action).addEventListener("click", () => run(action));
  $("write").addEventListener("submit", (event) => {
    event.preventDefault();
    run("email", {email: {sender: $("sender").value, subject: $("subject").value, body: $("body").value}});
  });
  $("reset").addEventListener("click", startOver);
  render(EMPTY);
  try {
    config = await api("api/config");
  } catch (error) {
    config = {live: false};
  }
  const dot = document.querySelector("#live-fact .dot");
  dot.classList.add(config.live ? "on" : "off");
  $("live-text").textContent = config.live
    ? "Live on Nebius Token Factory: Nemotron 3 Nano sorts each email and Nemotron 3 Super writes replies."
    : "Not connected to Nebius Token Factory right now, so the model steps are switched off.";
  if (config.clock) $("clock-text").textContent = `The demo clock reads ${when(config.clock)}, the week of the story, so its deadlines are still ahead.`;
  if (config.per_session) $("write-note").textContent = `Each visit can make up to ${config.per_session} model calls.`;
  try {
    const state = await api(`api/state?session=${session}`);
    if (state.busy && state.job) {
      setBusy(true);
      showNotice("Keeper is finishing your last step.");
      try { await follow(state.job); showNotice(""); } finally { setBusy(false); }
    } else {
      render(state);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
}

init();
