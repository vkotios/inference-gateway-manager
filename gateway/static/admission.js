"use strict";
// Chat admission panel: slots, queue, operator actions and settings. Everything shown comes from
// /api/status "admission"; client names and reasons are rendered as text, never markup.
let admissionView = null, admissionBusy = false, admissionFormKey = null, admissionDirty = false;
const ADMISSION_DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];
const admissionNum = (v, d = 1) => Number(v).toLocaleString(undefined, {maximumFractionDigits: d});
function buildAdmissionDays() {
  const box = $("adm-days");
  if (box.childElementCount) return;
  for (const [value, label] of ADMISSION_DAYS) {
    const item = document.createElement("label"); item.className = "check";
    const input = document.createElement("input"); input.type = "checkbox"; input.value = value; input.dataset.day = value;
    item.append(input, document.createTextNode(label)); box.append(item);
  }
}
function updateAdmissionButtons() {
  const ready = authenticated && admissionView && !admissionBusy;
  $("admission-pause").disabled = !ready;
  $("admission-reserve").disabled = !ready;
  $("admission-release").disabled = !ready;
  $("admission-calendar").disabled = !ready || !admissionView?.reservation_override;
  $("admission-calendar").hidden = !admissionView?.reservation_override;
  $("admission-fields").disabled = !ready;
  for (const button of document.querySelectorAll("#admission-card [data-admission-action]")) button.disabled = !ready;
}
function clearAdmission() {
  admissionView = null; admissionBusy = false; admissionFormKey = null; admissionDirty = false;
  $("admission-badge").textContent = "UNKNOWN";
  $("admission-summary").textContent = "Sign in to see chat slots and the queue.";
  $("admission-counters").textContent = ""; $("admission-status").textContent = "";
  $("admission-slots").replaceChildren(); $("admission-queue").replaceChildren();
  $("admission-pause").textContent = "Pause batch";
  updateAdmissionButtons();
}
function admissionRow(container, text, label, handler) {
  const row = document.createElement("div"); row.className = "arow";
  const span = document.createElement("span"); span.className = "grow"; span.textContent = text; row.append(span);
  if (handler) {
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary";
    button.dataset.admissionAction = label; button.textContent = label; button.onclick = handler; row.append(button);
  }
  container.append(row);
}
function describeCounters(v) {
  const c = v.counters, w = v.wait_ms, ms = x => x == null ? "—" : admissionNum(x / 1000) + " s";
  return `Started: ${c.interactive.admitted} interactive, ${c.batch.admitted} batch · waited: ${c.interactive.waited}, ${c.batch.waited}`
    + ` · wait p95: ${ms(w.interactive.p95)} interactive, ${ms(w.batch.p95)} batch`
    + ` · timed out: ${c.interactive.timeouts + c.batch.timeouts} · refused full: ${c.interactive.queue_full + c.batch.queue_full}`
    + ` · dropped: ${c.interactive.dropped + c.batch.dropped} · aborted: ${v.aborted}`;
}
function showAdmission(view) {
  admissionView = view || null;
  if (!view) {
    $("admission-badge").textContent = "NO MODEL";
    $("admission-summary").textContent = "No chat model is loaded, so there is nothing to admit.";
    $("admission-slots").replaceChildren(); $("admission-queue").replaceChildren(); $("admission-counters").textContent = "";
    updateAdmissionButtons(); return;
  }
  $("admission-badge").textContent = view.mode === "single" ? "ONE AT A TIME" : view.paused_batch ? "BATCH PAUSED" : "QUEUING";
  const reservation = view.reservation_override
    ? `${view.reserved_now} reserved for interactive (set by you until midnight)`
    : `${view.reserved_now} reserved for interactive (calendar)`;
  $("admission-summary").textContent = `${view.model} · ${view.slots_total} chat slot${view.slots_total === 1 ? "" : "s"} · `
    + (view.mode === "single" ? "requests are refused when no slot is free" : `${reservation} · batch ${view.paused_batch ? "paused" : "running"}`)
    + (view.state_error ? " · " + view.state_error : "");
  $("admission-pause").textContent = view.paused_batch ? "Resume batch" : "Pause batch";
  const slots = $("admission-slots"), queue = $("admission-queue");
  slots.replaceChildren(); queue.replaceChildren();
  if (!view.running.length) admissionRow(slots, "No request is generating.");
  for (const r of view.running) {
    admissionRow(slots,
      `${r.client} · ${r.priority}${r.borrowed ? " · using the reserved slot" : ""} · ${admissionNum(r.elapsed_seconds)} s · ${r.tokens == null ? "no" : admissionNum(r.tokens, 0)} tokens so far${r.aborting ? " · aborting" : ""}`,
      "Abort…", r.aborting ? null : () => abortRequest(r));
  }
  if (!view.queue.length) admissionRow(queue, "Nothing is waiting.");
  view.queue.forEach((q, index) => {
    admissionRow(queue,
      `${index + 1}. ${q.client} · ${q.priority} · waiting ${admissionNum(q.waited_seconds)} s · expected ${q.expected_tokens == null ? "length unknown" : admissionNum(q.expected_tokens, 0) + " tokens (" + q.expected_source.replace("_", " ") + ")"}${q.short ? " · short" : ""}`,
      "Drop…", () => dropRequest(q));
  });
  $("admission-counters").textContent = describeCounters(view);
  fillAdmissionForm(view);
  updateAdmissionButtons();
}
async function admissionCall(path, body, done) {
  if (!authenticated || admissionBusy) return;
  const epoch = generation; admissionBusy = true; updateAdmissionButtons();
  try { await api(path, body); if (epoch === generation) { $("admission-status").textContent = done; await poll(); } }
  catch (error) { if (epoch === generation) $("admission-status").textContent = error.message; }
  finally { if (epoch === generation) { admissionBusy = false; updateAdmissionButtons(); } }
}
async function abortRequest(r) {
  if (!await askConfirmation("Abort this running request?",
    `This ends the generation for ${r.client} now. Its agent sees an aborted_by_operator error, its current turn breaks, and orch records the abort on the run. Nothing is retried automatically.`, "Abort request", true)) return;
  const reason = $("confirm-reason").value.trim();
  await admissionCall(`/api/admission/requests/${encodeURIComponent(r.id)}/abort`, {reason}, "Request aborted. The client was told why.");
}
async function dropRequest(q) {
  if (!await askConfirmation("Drop this waiting request?",
    `${q.client} is told to retry later (429 with Retry-After). Nothing has started yet, so no work is lost.`, "Drop request", true)) return;
  const reason = $("confirm-reason").value.trim();
  await admissionCall(`/api/admission/queue/${encodeURIComponent(q.id)}/drop`, {reason}, "Request dropped. The client was told to retry later.");
}
$("admission-pause").onclick = () => admissionCall("/api/admission/pause", {paused: !admissionView?.paused_batch},
  admissionView?.paused_batch ? "Batch resumed." : "Batch paused. Running requests finish; interactive requests are unaffected.");
$("admission-reserve").onclick = () => admissionCall("/api/admission/reservation", {value: Math.max(1, dayReserved(admissionView?.settings))},
  "Slot reserved for interactive use until midnight.");
$("admission-release").onclick = () => admissionCall("/api/admission/reservation", {value: 0}, "Reservation released until midnight.");
$("admission-calendar").onclick = () => admissionCall("/api/admission/reservation", {value: null}, "Following the calendar again.");

// ---- settings form -------------------------------------------------------------------------
function dayReserved(settings) {
  const r = settings?.reservedInteractive;
  if (typeof r === "number") return r;
  return r?.windows?.[0]?.reserved ?? r?.default ?? 0;
}
function fillAdmissionForm(view) {
  buildAdmissionDays();
  const key = view.model + "|" + JSON.stringify(view.settings);
  if (key === admissionFormKey || admissionDirty) return;
  admissionFormKey = key;
  const s = view.settings, r = s.reservedInteractive, window = typeof r === "object" ? r.windows?.[0] : null;
  $("adm-mode").value = s.mode;
  $("adm-reserved-day").value = dayReserved(s);
  $("adm-reserved-night").value = typeof r === "number" ? r : r.default ?? 0;
  $("adm-from").value = window?.start || "08:00"; $("adm-to").value = window?.end || "20:00";
  $("adm-tz").value = typeof r === "object" ? r.timezone || "" : "";
  const days = new Set(window?.days || ["mon", "tue", "wed", "thu", "fri"]);
  for (const box of $("adm-days").querySelectorAll("input")) box.checked = days.has(box.value);
  $("adm-maxq").value = s.maxQueue; $("adm-int-timeout").value = s.interactiveTimeoutS; $("adm-bat-timeout").value = s.batchTimeoutS;
  $("adm-borrow").value = s.borrowShortTokens;
  for (const size of ["XS", "S", "M"]) $("adm-" + size.toLowerCase()).value = s.sizeClassExpectedTokens[size];
  $("adm-help").textContent = `This model has ${view.slots_total} chat slot${view.slots_total === 1 ? "" : "s"}, so requests ${view.mode === "single" ? "are refused when none is free" : "queue when none is free"}. `
    + "Nothing is ever cut short: the short-request limit only decides which batch requests may use a reserved slot. Changes apply to new requests at once and are kept across restarts.";
}
function readAdmissionForm() {
  const int = (id, label) => {
    const value = Number($(id).value);
    if (!Number.isInteger(value) || $(id).value === "") throw new Error(label + " must be a whole number.");
    return value;
  };
  const day = int("adm-reserved-day", "Reserved slots in working hours"), night = int("adm-reserved-night", "Reserved slots outside working hours");
  let reserved = night;
  if (day !== night) {
    const days = [...$("adm-days").querySelectorAll("input:checked")].map(box => box.value);
    if (!days.length || !$("adm-from").value || !$("adm-to").value) throw new Error("Choose working days and hours for the reservation.");
    reserved = {default: night, windows: [{days, start: $("adm-from").value, end: $("adm-to").value, reserved: day}]};
    if ($("adm-tz").value.trim()) reserved.timezone = $("adm-tz").value.trim();
  }
  return {
    mode: $("adm-mode").value, reservedInteractive: reserved, maxQueue: int("adm-maxq", "Max waiting requests"),
    interactiveTimeoutS: int("adm-int-timeout", "Interactive wait limit"), batchTimeoutS: int("adm-bat-timeout", "Batch wait limit"),
    borrowShortTokens: int("adm-borrow", "Short request limit"),
    sizeClassExpectedTokens: {XS: int("adm-xs", "XS tokens"), S: int("adm-s", "S tokens"), M: int("adm-m", "M tokens")},
  };
}
$("admission-form").addEventListener("input", () => { admissionDirty = true; });
$("admission-form").onsubmit = async event => {
  event.preventDefault();
  if (!authenticated || admissionBusy) return;
  let settings;
  try { settings = readAdmissionForm(); } catch (error) { $("admission-status").textContent = error.message; return; }
  admissionDirty = false; admissionFormKey = null;
  await admissionCall("/api/admission/settings", {settings}, "Admission settings saved.");
};
$("adm-reset").onclick = async () => {
  if (!authenticated || admissionBusy) return;
  if (!await askConfirmation("Return to the configured admission settings?", "Settings saved from this page are removed for this model. The administrator's configuration applies again.", "Use configuration")) return;
  admissionDirty = false; admissionFormKey = null;
  await admissionCall("/api/admission/settings", {settings: null}, "Using the configured admission settings.");
};
