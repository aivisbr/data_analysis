"use strict";

(() => {
  const el = (id) => document.getElementById(id);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const percent = (value) => value === null ? "N/A" : new Intl.NumberFormat("lv-LV", {style: "percent", minimumFractionDigits: 0, maximumFractionDigits: 2}).format(value);
  const variantName = (v) => v === "raw" ? "oriģināli" : "laboti";
  let catalog = {uploads: [], managed: []}, history = [], busy = false;
  const chosen = new Set();
  const remember = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  const recalled = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const status = (message, error = false) => { el("evaluation-status").textContent = message; el("evaluation-status").classList.toggle("error-text", error); };
  async function request(path, method = "GET", body) {
    let response;
    try {
      response = await fetch(path, {method, cache: "no-store", signal: AbortSignal.timeout(120000),
        ...(body === undefined ? {} : body instanceof FormData ? {body} : {body: JSON.stringify(body), headers: {"Content-Type": "application/json"}})});
    } catch { throw new Error("Nav savienojuma vai aprēķins pārsniedza gaidīšanas laiku. Atjauno vēsturi, lai pārbaudītu saglabāto izpildi."); }
    if (!response.ok) {
      let message = "Darbību nevar izpildīt.";
      try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch {}
      throw new Error(message);
    }
    try { return await response.json(); }
    catch { throw new Error("Lietotnes atbildi nevar nolasīt. Atjauno failus un vēsturi."); }
  }
  async function work(action) {
    if (busy) return;
    busy = true; el("evaluation-controls").disabled = true;
    try { await action(); } catch (error) { status(error.message, true); }
    finally { busy = false; el("evaluation-controls").disabled = false; }
  }
  function sources() {
    return [...catalog.uploads.filter((item) => item.role === "hypothesis").map((item) => ({...item, selection: {kind: "upload", input_id: item.id}})), ...catalog.managed];
  }
  const selectionKey = (selection) => JSON.stringify(selection);
  function filteredSources() { return sources().filter((item) => item.reading === el("eval-reading").value && (item.variant || item.selection.variant) === el("eval-variant").value); }
  function renderSources() {
    const list = el("hypothesis-list"); list.replaceChildren();
    const items = filteredSources();
    if (!items.length) list.append(node("p", "Šim lasījumam un veidam vēl nav teksta failu. Pievieno transkriptus.", "hint"));
    for (const item of items) {
      const label = node("label", undefined, "hypothesis-row");
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = chosen.has(selectionKey(item.selection));
      checkbox.setAttribute("aria-label", `Vērtēt: ${item.label}`);
      checkbox.addEventListener("change", () => { const key = selectionKey(item.selection); if (checkbox.checked) chosen.add(key); else chosen.delete(key); updateCount(); });
      const info = node("span"); info.append(node("strong", item.label));
      if (item.processing_status && item.processing_status !== "complete") info.append(node("span", " Nepilnīga vai neveiksmīga transkripcija", "error-text"));
      info.append(node("span", `${item.reading} · ${variantName(item.variant || item.selection.variant)}${item.attempt_number ? ` · mēģinājums ${item.attempt_number}` : ""}`, "hint"));
      info.append(node("span", item.disk_path, "path-text")); label.append(checkbox, info); list.append(label);
    }
    updateCount();
  }
  function updateCount() { const count = filteredSources().filter((item) => chosen.has(selectionKey(item.selection))).length; el("hypothesis-count").textContent = `Izvēlēti ${count} transkripti.`; }
  function showGroundTruthPath() { const item = catalog.uploads.find((item) => item.id === el("gt-select").value); el("gt-path").textContent = item ? item.disk_path : ""; }
  function renderCatalog() {
    const selected = el("gt-select").value || recalled("p7-ground-truth");
    el("gt-select").replaceChildren(new Option("Izvēlies etalonu…", ""));
    for (const item of catalog.uploads.filter((item) => item.role === "ground_truth")) el("gt-select").add(new Option(item.label, item.id));
    if (catalog.uploads.some((item) => item.role === "ground_truth" && item.id === selected)) el("gt-select").value = selected;
    showGroundTruthPath(); renderSources();
  }
  function renderHistory(selected = el("evaluation-history").value || recalled("p7-last-evaluation")) {
    el("evaluation-history").replaceChildren(new Option("Izvēlies saglabātu vērtējumu…", ""));
    for (const item of history) {
      const text = item.valid ? `${new Date(item.created_at).toLocaleString("lv-LV")} · ${item.reading} · ${variantName(item.variant)} · ${item.hypothesis_count} faili · ${item.id.slice(0, 8)}${item.complete ? "" : " · aprēķins nav pabeigts"}` : `${item.id} · dati nav pieejami`;
      const option = new Option(text, item.id); option.disabled = !item.valid; el("evaluation-history").add(option);
    }
    if (history.some((item) => item.id === selected && item.valid)) el("evaluation-history").value = selected;
  }
  async function refresh() {
    const results = await Promise.all([request("/api/evaluation-catalog"), request("/api/evaluations")]);
    [catalog, history] = results; renderCatalog(); renderHistory();
    if (catalog.unavailable_runs.length) status("Dažu izpilžu transkripti nav pieejami; pārējie faili ir ielādēti.", true);
  }
  async function importFiles(groundTruth) {
    const files = Array.from(el(groundTruth ? "gt-file" : "hyp-files").files);
    if (!files.length) throw new Error("Vispirms izvēlies .txt failu.");
    let imported = 0, errors = [], lastGt;
    for (const file of files) {
      status(`Saglabā ${file.name}…`);
      const form = new FormData(); form.append("file", file); form.append("role", groundTruth ? "ground_truth" : "hypothesis");
      if (!groundTruth) { form.append("reading", el("eval-reading").value); form.append("variant", el("eval-variant").value); }
      try {
        const item = await request("/api/evaluation-inputs", "POST", form); imported += 1;
        if (groundTruth) lastGt = item.id; else chosen.add(selectionKey({kind: "upload", input_id: item.id}));
      } catch (error) { errors.push(`${file.name}: ${error.message}`); }
    }
    await refresh();
    if (lastGt) { el("gt-select").value = lastGt; remember("p7-ground-truth", lastGt); showGroundTruthPath(); }
    el(groundTruth ? "gt-file" : "hyp-files").value = "";
    status(`Saglabāti ${imported} faili.${errors.length ? ` ${errors.join(" ")}` : ""}`, errors.length > 0);
  }
  function appendTable(parent, headers, rows) {
    const wrap = node("div", undefined, "table-scroll"), table = node("table"), head = node("thead"), tr = node("tr");
    for (const heading of headers) { const th = node("th", heading); th.scope = "col"; tr.append(th); }
    head.append(tr); table.append(head);
    const body = node("tbody"); for (const row of rows) { const tr = node("tr"); for (const value of row) tr.append(node("td", value)); body.append(tr); } table.append(body); wrap.append(table); parent.append(wrap);
  }
  function pagedIssues(parent, headers, rows) {
    let shown = 0; const slot = node("div"), button = node("button", "Rādīt vairāk atšķirību"); button.type = "button";
    const render = () => { shown += 200; slot.replaceChildren(); appendTable(slot, headers, rows.slice(0, shown)); button.hidden = shown >= rows.length; };
    button.addEventListener("click", render); parent.append(slot, button); render();
  }
  function diffText(text, changes, side) {
    const container = node("p", undefined, "diff-text"), chars = Array.from(text); let cursor = 0;
    for (const change of changes) {
      const start = change[`${side}_start`], end = change[`${side}_end`];
      container.append(document.createTextNode(chars.slice(cursor, start).join("")));
      if (end > start) container.append(node(side === "reference" ? "del" : "ins", chars.slice(start, end).join("")));
      cursor = end;
    }
    container.append(document.createTextNode(chars.slice(cursor).join("")));
    if (!text) container.append(node("span", "(tukšs transkripts)", "hint"));
    return container;
  }
  function detailBody(parent, result, index) {
    const input = result.snapshot.hypotheses[index], d = result.diagnostics.scores[index].diagnostics, legacy = result.legacy.scores[index];
    const paths = result.paths.inputs[index + 1];
    parent.append(node("p", `Avota fails: ${paths.source || "vairs nav pieejams"}`, "path-text"), node("p", `Saglabātā ievade: ${paths.snapshot}`, "path-text"), node("p", `SHA-256: ${input.sha256}`, "path-text"));
    parent.append(node("p", `WER: aizvietojumi ${legacy.wer.substitutions}, dzēsumi ${legacy.wer.deletions}, iestarpinājumi ${legacy.wer.insertions}. CER: aizvietojumi ${legacy.cer.substitutions}, dzēsumi ${legacy.cer.deletions}, iestarpinājumi ${legacy.cer.insertions}.`, "hint"));
    if (d.has_repeated_lexical_tokens) parent.append(node("p", "Tekstā atkārtojas vārdi; saskaņojums var būt neviennozīmīgs.", "notice"));
    parent.append(node("h4", "Teksta atšķirības"), node("p", "Sarkans: trūkstošs vai aizvietots etalona teksts. Zaļš: pievienots vai aizvietots transkripta teksts. Skats saglabā burtu reģistru un pieturzīmes, bet izlīdzina atstarpes un Unicode pierakstu.", "hint"));
    const grid = node("div", undefined, "diff-grid"), reference = node("div"), hypothesis = node("div");
    reference.append(node("h4", "Etalons"), diffText(result.texts.strict_reference, d.character_changes, "reference"));
    hypothesis.append(node("h4", "Transkripts"), diffText(result.texts.strict_hypotheses[index], d.character_changes, "hypothesis")); grid.append(reference, hypothesis); parent.append(grid);
    parent.append(node("h4", "Pieturzīmes pēc veida"));
    appendTable(parent, ["Zīme", "Pareizas", "Papildu", "Trūkst", "Precizitāte", "Pilnīgums", "F1"], d.punctuation.per_mark.map((p) => [p.mark, p.true_positive, p.false_positive, p.false_negative, percent(p.precision), percent(p.recall), percent(p.f1)]));
    parent.append(node("p", `Saskaņotas ${d.punctuation.matched_boundaries} no ${d.punctuation.reference_boundaries} etalona vārdu robežām. Pie nepareiziem/trūkstošiem vārdiem pieturzīmes var palikt nesaskaņotas.`, "hint"));
    const boundary = (index) => index === null ? "nesaskaņota" : index === 0 ? "pirms 1. vārda" : `pēc ${index}. vārda`;
    if (d.punctuation.issues.length) pagedIssues(parent, ["Etalona pozīcija", "Transkripta pozīcija", "Sagaidīts", "Pierakstīts"], d.punctuation.issues.map((issue) => [boundary(issue.reference_boundary), boundary(issue.hypothesis_boundary), issue.expected || "—", issue.actual || "—"]));
    parent.append(node("h4", "Lielie un mazie burti"), node("p", `Pareizi ${d.capitalization.correct} no ${d.capitalization.eligible} salīdzināmiem vārdiem. Burtu reģistra tvērums: ${percent(d.capitalization.coverage)}; leksiskais tvērums: ${percent(d.capitalization.lexical_coverage)}. Aizvietoti/trūkstoši un bezreģistra vārdi neietilpst nosacītās precizitātes saucējā.`, "hint"));
    if (d.capitalization.incorrect.length) pagedIssues(parent, ["Etalona vārds", "Transkripta vārds", "Sagaidīts", "Pierakstīts"], d.capitalization.incorrect.map((issue) => [issue.reference_index + 1, issue.hypothesis_index + 1, issue.expected, issue.actual]));
    else parent.append(node("p", "Salīdzināmajos vārdos burtu reģistra atšķirību nav.", "hint"));
  }
  function renderResults(result) {
    const output = el("evaluation-output"); output.replaceChildren(); output.hidden = false;
    const snapshot = result.snapshot;
    output.append(node("p", "SAGLABĀTS VĒRTĒJUMS", "eyebrow"), node("h3", `${snapshot.reading} · ${snapshot.reading === "A" ? "Try1" : "Try2"} · ${variantName(snapshot.variant)}`), node("p", `${new Date(snapshot.created_at).toLocaleString("lv-LV")} · ${snapshot.id}`, "path-text"));
    output.append(node("p", `Etalons: ${snapshot.ground_truth.label}`, "hint"), node("p", `Avots: ${result.paths.inputs[0].source || "vairs nav pieejams"}`, "path-text"), node("p", `Rezultāti: ${result.paths.output_directory}`, "path-text"));
    const exports = node("div", undefined, "export-links");
    for (const kind of ["json", "csv"]) { const link = node("a", `Lejupielādēt ${kind.toUpperCase()}`); link.href = `/api/evaluations/${snapshot.id}/exports/${kind}`; link.download = `diktats_${snapshot.id}.${kind}`; exports.append(link); } output.append(exports);
    appendTable(output, ["Pieraksts", "WER ↓", "CER ↓", "Stingrais CER ↓", "Pieturzīmju F1 ↑", "Burtu reģistrs ↑", "Tvērums", "Precīzi"], snapshot.hypotheses.map((item, index) => {
      const legacy = result.legacy.scores[index], d = result.diagnostics.scores[index].diagnostics;
      return [item.label, percent(legacy.wer.rate), percent(legacy.cer.rate), percent(d.strict_cer.rate), percent(d.punctuation.totals.f1), `${percent(d.capitalization.accuracy)} (${d.capitalization.correct}/${d.capitalization.eligible})`, percent(d.capitalization.coverage), d.exact_match ? "Jā" : "Nē"];
    }));
    output.append(node("p", "WER/CER ir kļūdu īpatsvars; mazāk ir labāk un vērtība var pārsniegt 100 %. Burtu reģistra precizitāte attiecas tikai uz salīdzināmiem vārdiem — skati arī tvērumu. N/A: nav saucēja. Pieturzīmju diagnostika izmanto vienādu blakusvārdu robežas un precīzu tipogrāfiju; tā nav LSM vērtēšanas skala.", "hint"));
    for (let index = 0; index < snapshot.hypotheses.length; index++) {
      const details = node("details", undefined, "result-detail"), summary = node("summary", `Atšķirības: ${snapshot.hypotheses[index].label}`), body = node("div");
      details.append(summary, body); let built = false;
      details.addEventListener("toggle", () => { if (details.open && !built) { detailBody(body, result, index); built = true; } }); output.append(details);
    }
    output.append(node("p", `WER/CER: ${result.legacy.scorer_version} · diagnostika: ${result.diagnostics.scorer_version} · jiwer ${result.legacy.jiwer_version} · RapidFuzz ${result.diagnostics.rapidfuzz_version}`, "hint"));
    remember("p7-last-evaluation", snapshot.id); el("evaluation-history").value = snapshot.id;
  }
  el("import-gt").addEventListener("click", () => work(() => importFiles(true)));
  el("import-hyp").addEventListener("click", () => work(() => importFiles(false)));
  el("gt-select").addEventListener("change", () => { remember("p7-ground-truth", el("gt-select").value); showGroundTruthPath(); });
  for (const id of ["eval-reading", "eval-variant"]) el(id).addEventListener("change", renderSources);
  el("refresh-evaluations").addEventListener("click", () => work(async () => { status("Ielādē failus un vēsturi…"); await refresh(); if (!catalog.unavailable_runs.length) status("Faili un vēsture ir atjaunoti."); }));
  el("calculate-evaluation").addEventListener("click", () => work(async () => {
    const groundTruth = el("gt-select").value, hypotheses = filteredSources().filter((item) => chosen.has(selectionKey(item.selection))).map((item) => item.selection);
    if (!groundTruth || !hypotheses.length) throw new Error("Izvēlies etalonu un vismaz vienu transkriptu.");
    status("Nolasa failus un saglabā ievades momentuzņēmumu…");
    const snapshot = await request("/api/evaluations", "POST", {ground_truth: {kind: "upload", input_id: groundTruth}, hypotheses}); remember("p7-last-evaluation", snapshot.id);
    status("Aprēķina WER/CER un pieturzīmju/burtu reģistra diagnostiku…");
    try { const result = await request(`/api/evaluations/${snapshot.id}/results`, "POST"); await refresh(); renderResults(result); status("Aprēķins un JSON/CSV eksporti ir saglabāti diskā."); }
    catch (error) { await refresh(); renderHistory(snapshot.id); throw error; }
  }));
  el("open-evaluation").addEventListener("click", () => work(async () => {
    const id = el("evaluation-history").value; if (!id) throw new Error("Izvēlies saglabātu vērtējumu.");
    status("Atver vai pabeidz saglabāto aprēķinu…"); const result = await request(`/api/evaluations/${id}/results`, "POST"); await refresh(); renderResults(result); status("Atvērts saglabātais vērtējums; pašreizējie avota labojumi to nemaina.");
  }));
  work(async () => {
    status("Ielādē vērtēšanas failus…"); await refresh();
    const last = recalled("p7-last-evaluation");
    if (last && history.some((item) => item.id === last && item.valid && item.complete)) {
      renderResults(await request(`/api/evaluations/${last}/results`)); selectTab(el("tab-results"));
    }
    if (!catalog.unavailable_runs.length) status("Izvēlies failus vai atver saglabātu vērtējumu.");
  });
})();
