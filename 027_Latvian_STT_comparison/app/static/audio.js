/* Local preparation only; provider orchestration is a later task. */
(() => {
  const el = id => document.getElementById(id);
  let uploads = [];
  const remembered = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const remember = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  async function api(url, options = {}) {
    const response = await fetch(url, {...options, signal: AbortSignal.timeout(240000)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Audio apstrādi nevar izpildīt.');
    return body;
  }
  async function busy(work) {
    el('audio-controls').disabled = true;
    try { await work(); } catch (error) {
      el('audio-status').textContent = error.name === 'TimeoutError'
        ? 'Audio apstrādes laiks pārsniegts. Atjauno lapu, lai pārbaudītu saglabātos ierakstus.' : error.message;
    } finally { el('audio-controls').disabled = false; }
  }
  function selection() {
    const item = uploads.find(x => x.id === el('audio-select').value);
    el('audio-preview').pause();
    el('audio-prepared').hidden = true;
    if (!item) { el('audio-preview').removeAttribute('src'); return; }
    remember('p7-audio-id', item.id);
    el('audio-preview').src = `/api/audio-inputs/${item.id}/files/preview`;
    el('audio-info').textContent = `${item.filename} · ${item.duration_seconds.toLocaleString('lv-LV')} s · ${item.codec}`;
    el('audio-start').value = '0';
    el('audio-end').value = String(item.duration_seconds).replace('.', ',');
    el('audio-prepared').hidden = true;
  }
  async function refresh(selected) {
    uploads = await api('/api/audio-inputs');
    el('audio-select').replaceChildren(new Option('Izvēlies ierakstu', ''));
    for (const item of uploads) el('audio-select').add(new Option(item.filename, item.id));
    el('audio-select').value = selected || remembered('p7-audio-id') || '';
    selection();
  }
  function showPrepared(data) {
    el('audio-prepared').hidden = false;
    el('interval-preview').src = `/api/audio-preparations/${data.id}/preview`;
    const count = data.chunks.length;
    const noun = count % 10 === 1 && count % 100 !== 11 ? 'fragments' : 'fragmenti';
    el('audio-prepared-info').textContent = `${data.reading} · ${data.directory} · ${data.source_interval.start_seconds.toLocaleString('lv-LV')}–${data.source_interval.end_seconds.toLocaleString('lv-LV')} s · ${count} ${noun} bez pārklāšanās. Sagataves ID: ${data.id}`;
    el('audio-status').textContent = 'Intervāls saglabāts. Izpilde izmantos tieši šo sagatavi; lauku maiņa prasa jaunu sagatavošanu.';
    el('audio-prepared').dataset.preparationId = data.id;
    remember('p7-audio-preparation', data.id);
  }
  el('audio-select').addEventListener('change', selection);
  el('upload-audio').addEventListener('click', () => busy(async () => {
    const file = el('audio-file').files[0];
    if (!file) throw new Error('Izvēlies audio failu.');
    if (file.size > 250000000) throw new Error('Fails pārsniedz 250 MB.');
    el('audio-status').textContent = 'Saglabā un pārbauda audio…';
    const body = new FormData(); body.append('file', file);
    const item = await api('/api/audio-inputs', {method: 'POST', body});
    await refresh(item.id);
    el('audio-status').textContent = 'Oriģināls saglabāts. Noklausies ierakstu un izvēlies intervālu.';
  }));
  el('prepare-audio').addEventListener('click', () => busy(async () => {
    if (!el('audio-select').value) throw new Error('Izvēlies saglabātu audio ierakstu.');
    const number = id => Number(el(id).value.trim().replace(',', '.'));
    const start = number('audio-start');
    const end = el('audio-end').value.trim() ? number('audio-end') : null;
    if (!Number.isFinite(start) || start < 0 || (end !== null && (!Number.isFinite(end) || end <= start)))
      throw new Error('Norādi derīgu sākumu un beigas sekundēs.');
    el('audio-status').textContent = 'Sagatavo kopīgo audio intervālu…';
    const data = await api('/api/audio-preparations', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({audio_id: el('audio-select').value, reading: el('audio-reading').value, start_seconds: start, end_seconds: end})});
    showPrepared(data);
  }));
  busy(async () => {
    await refresh();
    const saved = remembered('p7-audio-preparation');
    if (saved) {
      const data = await api(`/api/audio-preparations/${saved}`);
      if (data.audio_id === el('audio-select').value) {
        el('audio-reading').value = data.reading;
        el('audio-start').value = String(data.source_interval.start_seconds).replace('.', ',');
        el('audio-end').value = String(data.source_interval.end_seconds).replace('.', ',');
        showPrepared(data);
      }
    }
  });
})();
