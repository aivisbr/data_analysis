"use strict";

const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
function selectTab(selected) {
  document.querySelector(".registry-footer").hidden = selected.id === "tab-results";
  for (const tab of tabs) {
    const active = tab === selected;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
  }
}
for (const tab of tabs) {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(tab);
    let next;
    if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === "Home") next = tabs[0];
    if (event.key === "End") next = tabs[tabs.length - 1];
    if (next) { event.preventDefault(); selectTab(next); next.focus(); }
  });
}

async function checkConnection() {
  const indicator = document.getElementById("connection");
  try {
    const response = await fetch("/api/health", { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (!response.ok || (await response.json()).status !== "ok") throw new Error("health");
    indicator.textContent = "Savienojums darbojas";
    indicator.dataset.state = "ok";
  } catch {
    indicator.textContent = "Nav savienojuma ar lietotni";
    indicator.dataset.state = "error";
  }
}
checkConnection();
setInterval(checkConnection, 15000);

const byId = (id) => document.getElementById(id);
const defaults = {language_hint: "lv-LV", keywords: [], prompt: null, latency: null, formatting: "default", turn_detection: "default"};
let models = [], profiles = [], editing = null, dialogMode = "live", dialogBusy = false, deleteTarget = null;
let languagePreviewSequence = 0;
const providerNames = {openai: "OpenAI", gemini: "Gemini", elevenlabs: "ElevenLabs"};
const deliveryNames = {streaming: "Straumēšana", committed_turn: "Pēc runas posma", completed_file: "Audio fails"};
const supportHelp = (support) => support === "supported" ? "Pieejams šim profilam." : support === "unverified" ? "Atbalsts vēl nav pārbaudīts; izvēle ir atspējota." : "Šis profils šo iestatījumu neatbalsta.";

async function api(path, method = "GET", body) {
  let response;
  try {
    response = await fetch(path, {method, cache: "no-store", signal: AbortSignal.timeout(10000),
      ...(body === undefined ? {} : {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})});
  } catch { throw new Error("Nav savienojuma ar lietotni. Atjauno sarakstu un pārbaudi, vai izmaiņas ir saglabātas."); }
  if (!response.ok) {
    let message = "Darbību nevar izpildīt.";
    try { const error = await response.json(); if (typeof error.detail === "string") message = error.detail; } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function registryStatus(text, error = false) {
  byId("registry-status").textContent = text;
  byId("registry-status").classList.toggle("error-text", error);
}

function makeNode(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderModels() {
  for (const mode of ["live", "file"]) {
    const entries = models.filter((m) => m.mode === mode);
    const list = byId(`models-${mode}`);
    list.replaceChildren();
    byId(`count-${mode}`).textContent = `Ieslēgti ${entries.filter((m) => m.enabled).length} no ${entries.length}.`;
    if (!entries.length) list.append(makeNode("p", "Saraksts ir tukšs. Pievieno modeli, lai sagatavotu salīdzinājumu.", "hint"));
    for (const model of entries) {
      const profile = profiles.find((p) => p.id === model.capability_profile_id);
      const row = makeNode("article", undefined, `model-row${model.enabled ? "" : " inactive"}`);
      row.dataset.entryId = model.id;
      row.setAttribute("aria-label", model.display_name);
      const info = makeNode("div", undefined, "model-info");
      info.append(makeNode("h4", model.display_name));
      info.append(makeNode("p", `${providerNames[model.provider]} · ${model.model_id} · ${deliveryNames[profile?.delivery] || "Profils nav atrasts"}`, "model-identity"));
      const custom = JSON.stringify(model.settings) !== JSON.stringify(defaults);
      const summary = [`Valoda: ${model.settings.language_hint || "automātiski"}`, `${model.settings.keywords.length} atslēgvārdi`];
      if (model.settings.prompt) summary.push("Konteksta uzvedne");
      if (model.settings.latency) summary.push("Pielāgots latentums");
      if (model.settings.formatting !== "default") summary.push(model.settings.formatting === "verbatim" ? "Burtisks pieraksts" : "Attīrīts pieraksts");
      if (model.settings.turn_detection !== "default") summary.push(model.settings.turn_detection === "manual" ? "Manuāli posmi" : "Automātiski posmi");
      info.append(makeNode("p", summary.join(" · "), "model-summary"));
      if (custom) info.append(makeNode("span", "Individuāli iestatījumi", "settings-badge"));
      if (!profiles.some((p) => p.provider === model.provider && p.mode === model.mode && p.model_id === model.model_id)) {
        info.append(makeNode("p", "Jauns identifikators — saderība vēl nav pārbaudīta.", "hint"));
      }
      const actions = makeNode("div", undefined, "model-actions");
      const label = makeNode("label", undefined, "check-label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox"; checkbox.checked = model.enabled;
      checkbox.setAttribute("aria-label", `Izmantot modeli: ${model.display_name}`);
      checkbox.addEventListener("change", async () => {
        checkbox.disabled = true;
        try {
          const updated = await api(`/api/models/${model.id}`, "PATCH", {enabled: checkbox.checked});
          models = models.map((m) => m.id === updated.id ? updated : m);
          renderModels();
          byId(`models-${mode}`).querySelector(`[data-entry-id="${model.id}"] input`).focus();
          registryStatus("Modeļa izvēle saglabāta.");
        } catch (error) { checkbox.checked = model.enabled; checkbox.disabled = false; registryStatus(error.message, true); }
      });
      label.append(checkbox, document.createTextNode("Izmantot"));
      const edit = makeNode("button", "Iestatījumi"); edit.type = "button";
      edit.setAttribute("aria-label", `Iestatījumi: ${model.display_name}`);
      edit.addEventListener("click", () => openModel(mode, model));
      const remove = makeNode("button", "Dzēst", "delete-button"); remove.type = "button";
      remove.setAttribute("aria-label", `Dzēst modeli: ${model.display_name}`);
      remove.addEventListener("click", () => openDelete(model));
      actions.append(label, edit, remove); row.append(info, actions); list.append(row);
    }
  }
}

async function loadModels() {
  byId("refresh-models").disabled = true;
  try {
    const loaded = await Promise.all([api("/api/models"), api("/api/capabilities")]);
    [models, profiles] = loaded;
    renderModels(); registryStatus("Izvēle un iestatījumi glabājas šajā datorā.");
    document.querySelectorAll(".add-model").forEach((button) => { button.disabled = false; });
    return true;
  } catch (error) {
    registryStatus(error.message, true);
    document.querySelectorAll(".add-model").forEach((button) => { button.disabled = true; });
    return false;
  } finally { byId("refresh-models").disabled = false; }
}

function selectedProfile() { return profiles.find((p) => p.id === byId("model-profile").value); }
function populateProfiles(selectedId) {
  const options = profiles.filter((p) => p.provider === byId("model-provider").value && p.mode === dialogMode);
  byId("model-profile").replaceChildren(...options.map((p) => { const option = makeNode("option", p.model_id); option.value = p.id; return option; }));
  if (selectedId && options.some((p) => p.id === selectedId)) byId("model-profile").value = selectedId;
}

function setSettings(settings) {
  byId("language-auto").checked = settings.language_hint === null;
  byId("model-language").value = settings.language_hint || "";
  byId("model-keywords").value = settings.keywords.join("\n");
  byId("model-prompt").value = settings.prompt || "";
  byId("model-latency").value = settings.latency || "";
  byId("model-formatting").value = settings.formatting;
  byId("model-turns").value = settings.turn_detection;
}

async function previewLanguage() {
  const sequence = ++languagePreviewSequence;
  const hint = byId("language-auto").checked ? null : byId("model-language").value.trim();
  try {
    const result = await api("/api/language-preview", "POST", {provider: byId("model-provider").value, language_hint: hint});
    if (sequence === languagePreviewSequence) byId("language-help").textContent = hint === null ? "Nodrošinātājs noteiks valodu automātiski." : `ISO 639-1 vai BCP-47. Nodrošinātājam nosūtāmais kods: ${result.language_code}.`;
  } catch { if (sequence === languagePreviewSequence) byId("language-help").textContent = "Norādi ISO 639-1 vai BCP-47 kodu, piemēram, lv vai lv-LV."; }
}

function updateCapabilities() {
  const profile = selectedProfile();
  if (!profile) return;
  for (const [inputId, helpId, feature] of [["model-keywords", "keywords-help", "keywords"], ["model-prompt", "prompt-help", "prompt"], ["model-latency", "latency-help", "latency"], ["model-formatting", "formatting-help", "formatting"]]) {
    byId(inputId).disabled = profile[feature] !== "supported";
    byId(helpId).textContent = supportHelp(profile[feature]);
  }
  if (profile.keywords === "supported") byId("keywords-help").textContent = "Viena frāze katrā rindā. Šī profila pielāgotās vārdnīcas lauks ir pieejams.";
  if (profile.keywords === "supported" && profile.provider === "elevenlabs" && dialogMode === "live") byId("keywords-help").textContent = "Viena frāze katrā rindā; līdz 50 frāzēm, katra līdz 20 rakstzīmēm.";
  if (profile.keywords === "supported" && profile.provider === "gemini") byId("keywords-help").textContent = "Viena frāze katrā rindā; līdz 1000 frāzēm.";
  byId("model-turns").disabled = dialogMode === "file";
  byId("model-turns").querySelector('[value="automatic"]').disabled = profile.automatic_turn_detection !== "supported";
  byId("turns-help").textContent = dialogMode === "file" ? "Faila režīmā runas posmu vadība nav pieejama." : `Automātiska pabeigšana: ${supportHelp(profile.automatic_turn_detection)}`;
  byId("model-language").disabled = byId("language-auto").checked || profile.language !== "supported";
  byId("profile-notes").textContent = profile.notes.join(" ");
  updateCustomWarning(); previewLanguage();
}

function updateCustomWarning() {
  const known = profiles.some((p) => p.provider === byId("model-provider").value && p.mode === dialogMode && p.model_id === byId("model-id").value.trim());
  byId("custom-model-warning").hidden = known;
}

function openModel(mode, model = null) {
  editing = model; dialogMode = mode;
  byId("model-form").reset();
  byId("model-error").hidden = true;
  byId("model-dialog-title").textContent = model ? "Modeļa iestatījumi" : "Pievienot modeli";
  byId("dialog-mode").textContent = mode === "live" ? "TIEŠRAIDE" : "AUDIO FAILS";
  byId("model-provider").value = model?.provider || "openai";
  populateProfiles(model?.capability_profile_id);
  byId("model-id").value = model?.model_id || selectedProfile().model_id;
  byId("model-name").value = model?.display_name || byId("model-id").value;
  byId("model-order").value = model?.order ?? (Math.max(-1, ...models.filter((m) => m.mode === mode).map((m) => m.order)) + 1);
  setSettings(model?.settings || defaults); updateCapabilities();
  previousProfileId = byId("model-profile").value;
  byId("model-dialog").showModal(); byId("model-name").focus();
}

function showFormError(message) { byId("model-error").textContent = message; byId("model-error").hidden = false; }
function changeProfile(previousId) {
  const previous = profiles.find((p) => p.id === previousId);
  const next = selectedProfile();
  if (next && (!byId("model-id").value || byId("model-id").value === previous?.model_id)) {
    if (!byId("model-name").value || byId("model-name").value === previous?.model_id) byId("model-name").value = next.model_id;
    byId("model-id").value = next.model_id;
  }
  // Retain incompatible values rather than silently discarding them on profile change.
  updateCapabilities();
}
let previousProfileId = "";
byId("model-profile").addEventListener("focus", () => { previousProfileId = byId("model-profile").value; });
byId("model-profile").addEventListener("change", () => { changeProfile(previousProfileId); previousProfileId = byId("model-profile").value; });
byId("model-provider").addEventListener("change", () => { const previous = byId("model-profile").value; populateProfiles(); changeProfile(previous); previousProfileId = byId("model-profile").value; });
byId("model-id").addEventListener("input", () => { byId("custom-model-ack").checked = false; updateCustomWarning(); });
byId("model-language").addEventListener("change", previewLanguage);
byId("language-auto").addEventListener("change", updateCapabilities);
byId("reset-settings").addEventListener("click", () => { setSettings(defaults); updateCapabilities(); byId("model-error").hidden = true; });
document.querySelectorAll(".add-model").forEach((button) => { button.disabled = true; button.addEventListener("click", () => openModel(button.dataset.mode)); });
document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => { if (!dialogBusy) byId("model-dialog").close(); }));
byId("model-dialog").addEventListener("cancel", (event) => { if (dialogBusy) event.preventDefault(); });

byId("model-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (dialogBusy) return;
  byId("model-error").hidden = true;
  const profile = selectedProfile();
  const settings = {language_hint: byId("language-auto").checked ? null : byId("model-language").value.trim(),
    keywords: byId("model-keywords").value.split(/\r?\n/).map((term) => term.trim()).filter(Boolean),
    prompt: byId("model-prompt").value.trim() || null, latency: byId("model-latency").value || null,
    formatting: byId("model-formatting").value, turn_detection: byId("model-turns").value};
  const order = Number(byId("model-order").value);
  if (!profile || !byId("model-name").value.trim() || !byId("model-id").value.trim()) return showFormError("Norādi nosaukumu, identifikatoru un iespēju profilu.");
  if (!Number.isSafeInteger(order) || order < 0) return showFormError("Secībai jābūt veselam skaitlim, sākot ar nulli.");
  if (settings.language_hint === "") return showFormError("Norādi valodas kodu vai izvēlies automātisku noteikšanu.");
  const requested = {keywords: settings.keywords.length > 0, prompt: settings.prompt !== null, latency: settings.latency !== null, formatting: settings.formatting !== "default", automatic_turn_detection: settings.turn_detection === "automatic"};
  if (Object.entries(requested).some(([feature, used]) => used && profile[feature] !== "supported") || (dialogMode === "file" && settings.turn_detection !== "default")) {
    return showFormError("Saglabātie lauki nav saderīgi ar šo profilu. Izvēlies citu profilu vai atjauno iestatījumu noklusējumus.");
  }
  if (!byId("custom-model-warning").hidden && !byId("custom-model-ack").checked) return showFormError("Apstiprini, ka jaunā identifikatora saderība vēl nav pārbaudīta.");
  const body = {provider: byId("model-provider").value, model_id: byId("model-id").value.trim(), display_name: byId("model-name").value.trim(), mode: dialogMode, capability_profile_id: profile.id, order, settings};
  dialogBusy = true;
  const controlStates = Array.from(byId("model-form").querySelectorAll("input, select, textarea, button")).map((control) => [control, control.disabled]);
  controlStates.forEach(([control]) => { control.disabled = true; });
  byId("save-model").textContent = "Saglabā…";
  try {
    const saved = await api(editing ? `/api/models/${editing.id}` : "/api/models", editing ? "PATCH" : "POST", body);
    byId("model-dialog").close();
    if (await loadModels()) {
      registryStatus("Modeļa iestatījumi saglabāti.");
      byId(`models-${dialogMode}`).querySelector(`[data-entry-id="${saved.id}"] button`).focus();
    }
  } catch (error) { showFormError(error.message); }
  finally { dialogBusy = false; controlStates.forEach(([control, disabled]) => { control.disabled = disabled; }); byId("save-model").textContent = "Saglabāt"; }
});

function openDelete(model) {
  deleteTarget = model; byId("delete-name").textContent = model.display_name;
  byId("delete-error").hidden = true; byId("delete-dialog").showModal(); byId("cancel-delete").focus();
}
byId("cancel-delete").addEventListener("click", () => { if (!dialogBusy) byId("delete-dialog").close(); });
byId("delete-dialog").addEventListener("cancel", (event) => { if (dialogBusy) event.preventDefault(); });
byId("confirm-delete").addEventListener("click", async () => {
  if (dialogBusy) return; dialogBusy = true; byId("confirm-delete").disabled = true;
  try {
    await api(`/api/models/${deleteTarget.id}`, "DELETE"); byId("delete-dialog").close();
    if (await loadModels()) registryStatus("Modelis dzēsts no saraksta.");
    document.querySelector(`.add-model[data-mode="${deleteTarget.mode}"]`).focus();
  }
  catch (error) { byId("delete-error").textContent = error.message; byId("delete-error").hidden = false; }
  finally { dialogBusy = false; byId("confirm-delete").disabled = false; }
});
byId("refresh-models").addEventListener("click", loadModels);
loadModels();
