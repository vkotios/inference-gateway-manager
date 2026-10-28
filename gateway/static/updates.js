"use strict";
let updateView = null, updatePending = false;
const updateEngine = () => updateView?.engines.find(e => e.id === $("updates-engine").value);
function updateButtons() {
  const e = updateEngine(), busy = updatePending || updateView?.busy;
  $("updates-refresh").disabled = !authenticated || busy;
  $("updates-report").disabled = !authenticated || busy;
  $("updates-controls").disabled = !authenticated || !e || busy;
  $("updates-stage").disabled = !$("updates-version").value || updateView?.recovery_required;
  $("updates-apply").disabled = !$("updates-consent").checked || !e?.staged.some(s => s.revision === $("updates-version").value) || updateView?.recovery_required;
  $("updates-rollback").disabled = !$("updates-consent").checked || !(e?.rollback_available || updateView?.recovery_engine === e?.id);
  if (!authenticated) {
    updateView = null;
    $("updates-status").textContent = "Sign in to manage updates.";
    $("updates-report-body").value = ""; $("updates-report-link").removeAttribute("href");
    $("updates-report-panel").hidden = true;
    $("updates-consent").checked = false;
    $("updates-engine").replaceChildren(); $("updates-version").replaceChildren();
    $("updates-installed").textContent = ""; $("updates-tests").textContent = "";
  }
}
function renderUpdateEngine() {
  const e = updateEngine(), version = $("updates-version").value;
  $("updates-version").replaceChildren(); option($("updates-version"), "", "Choose a discovered version");
  for (const item of e?.available || []) option($("updates-version"), item.revision, item.name + " · " + item.revision.slice(0,12));
  if (e?.available.some(v => v.revision === version)) $("updates-version").value = version;
  $("updates-installed").textContent = e ? "Installed version: " + (e.installed_version || "unknown; check versions to inspect installation metadata") : "Ask the administrator to configure update recipes.";
  $("updates-tests").textContent = e ? "Authorized test selection: " + e.tests.map(t => t.model + " at " + t.context + " tokens").join(", ") + ". This is a compatibility smoke test, not an exhaustive model test." : "";
  updateButtons();
}
async function refreshUpdates() {
  if (!authenticated || updatePending) return;
  const epoch = generation;
  try {
    const result = await api("/api/updates");
    if (epoch !== generation) return;
    const previous = $("updates-engine").value;
    updateView = result; $("updates-engine").replaceChildren();
    for (const e of result.engines) option($("updates-engine"), e.id, e.id);
    if (result.engines.some(e => e.id === previous)) $("updates-engine").value = previous;
    $("updates-status").textContent = !result.enabled ? "Engine updates are disabled until an administrator adds recipes." : result.recovery_required ? "Recovery verification required for " + result.recovery_engine + ". Inference and normal lifecycle actions remain blocked." : result.job ? result.job.action + " · " + result.job.phase + (result.job.error ? " · " + result.job.error : "") : "Ready. Checking versions does not modify installed engines.";
    renderUpdateEngine();
  } catch (error) { if (epoch === generation) $("updates-status").textContent = error.message; }
}
async function performUpdate(action) {
  const e = updateEngine();
  if (!e || updatePending || !authenticated) return;
  const epoch = generation, revision = $("updates-version").value;
  if (action !== "stage") {
    if (!$("updates-consent").checked) return;
    const description = "Wait for idle maintenance; stop the current idle engine, load " + e.tests.map(t => t.model).join(", ") + ", verify short and streamed answers, and stop the test process. If verification fails, restore and verify the prior version. No previous workload is resumed automatically.";
    if (!await askConfirmation(action === "rollback" ? "Roll back and verify?" : "Test and activate this version?", description, action === "rollback" ? "Roll back" : "Test and activate")) return;
    if (epoch !== generation) return;
  }
  updatePending = true; updateButtons();
  try {
    await api("/api/updates/" + encodeURIComponent(e.id), {action, ...(action === "stage" || action === "apply" ? {revision} : {}), ...(action !== "stage" ? {test_models: e.tests.map(t => t.model)} : {})});
    if (epoch === generation) $("updates-consent").checked = false;
  } catch (error) { if (epoch === generation) $("updates-status").textContent = error.message; }
  finally { updatePending = false; if (epoch === generation) { updateButtons(); await refreshUpdates(); await refreshCatalog(state?.selected_engine); } }
}
$("updates-refresh").onclick = refreshUpdates;
$("updates-panel").ontoggle = () => { if ($("updates-panel").open) refreshUpdates(); };
$("updates-engine").onchange = () => { $("updates-consent").checked = false; renderUpdateEngine(); };
$("updates-version").onchange = () => { $("updates-consent").checked = false; updateButtons(); };
$("updates-consent").onchange = updateButtons;
$("updates-check").onclick = async () => {
  const e = updateEngine(), epoch = generation;
  if (!e || updatePending) return;
  updatePending = true; updateButtons();
  try { await api("/api/updates/" + encodeURIComponent(e.id) + "/check", {}); }
  catch (error) { if (epoch === generation) $("updates-status").textContent = error.message; }
  finally { updatePending = false; if (epoch === generation) await refreshUpdates(); }
};
$("updates-stage").onclick = () => performUpdate("stage");
$("updates-apply").onclick = () => performUpdate("apply");
$("updates-rollback").onclick = () => performUpdate("rollback");
function updateReportLink() {
  const url = new URL("https://github.com/vkotios/inference-gateway-manager/issues/new");
  url.searchParams.set("title", "Engine update compatibility report");
  url.searchParams.set("body", $("updates-report-body").value);
  $("updates-report-link").href = url.href;
}
$("updates-report").onclick = async () => {
  const epoch = generation;
  try {
    const report = await api("/api/updates/report");
    if (epoch !== generation) return;
    $("updates-report-body").value = report.body; $("updates-report-panel").hidden = false; updateReportLink();
  } catch (error) { if (epoch === generation) $("updates-status").textContent = error.message; }
};
$("updates-report-body").oninput = updateReportLink;
setInterval(() => { if (authenticated && $("updates-panel").open && !updatePending) refreshUpdates(); }, 2000);
updateButtons();
