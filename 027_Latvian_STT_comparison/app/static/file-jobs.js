(() => {
  const el = id => document.getElementById(id);
  let active = null, timer = null, busy = false;
  const labels = {queued:'Gaida',running:'Transkribē',complete:'Pabeigts',failed:'Kļūda',incomplete:'Nepilnīgs',interrupted:'Pārtraukts',cancelled:'Atcelts'};
  async function api(url, options={}) {
    const response = await fetch(url, {...options, signal:AbortSignal.timeout(30000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Izpildes kļūda.');
    return data;
  }
  async function action(work) {
    if (busy) return; busy=true;
    try { await work(); } catch (error) { el('file-job-status').textContent=error.message; }
    finally { busy=false; }
  }
  function node(tag, text) {const n=document.createElement(tag); if(text !== undefined)n.textContent=text;return n;}
  function render(data) {
    active=data.run_id; const out=el('file-job-output'); out.replaceChildren();
    const state = data.run.status === 'running' && !data.active_attempts.length && Object.keys(data.storage_failures).length
      ? 'Saglabāšanas kļūda' : (labels[data.run.status] || data.run.status);
    out.append(node('h3',`${data.execution_mode === 'demo' ? 'DEMO · ' : ''}${data.run.readings[0].label} · ${state}`));
    out.append(node('p',`Sagatave: ${data.preparation_id}`));
    const reading=data.run.readings[0];
    for(const model of data.run.models) {
      const attempts=reading.attempts.filter(a=>a.model_entry_id === model.entry_id);
      for(const attempt of attempts) {
        const card=node('article'); card.className='result-detail';
        const progress=data.attempts[attempt.id];
        card.append(node('h4',`${model.display_name} · mēģinājums ${attempt.number} · ${labels[attempt.status] || attempt.status}`));
        card.append(node('p',`${progress.completed_chunks}/${reading.segments.length} · saglabāšana: ${{pending:'gaida',saved:'saglabāts',failed:'kļūda'}[attempt.save_status]}`));
        for(const error of [...attempt.errors,...(progress.cleanup_errors || []),...progress.results.flatMap(r=>r.transcript.cleanup_errors || [])])card.append(node('p',error.message));
        if(data.storage_failures[attempt.id])card.append(node('p',data.storage_failures[attempt.id]));
        card.append(node('p',data.transcripts[attempt.id] || (['queued','running'].includes(attempt.status) ? 'Teksts vēl nav saņemts.' : 'Teksts nav saņemts.')));
        if(attempt.save_status === 'saved') {
          for(const [kind,label] of [['raw','Oriģināls .txt'],['editable','Rediģējamā kopija .txt']]) {
            const link=node('a',label);link.href=`/api/runs/${data.run_id}/attempts/${attempt.id}/files/${kind}`;card.append(link,node('span',' · '));
          }
          card.append(node('p',`runs/${data.run_id}/${reading.label === 'A' ? 'Try1' : 'Try2'}/editable/${attempt.editable_file.split(/[\\/]/).pop()}`));
        }
        if(attempt === attempts[attempts.length-1] && ['complete','failed','incomplete','interrupted'].includes(attempt.status)) {
          const retry=node('button','Atkārtot modeli');retry.type='button';
          retry.addEventListener('click',()=>action(async()=>render(await api(`/api/file-jobs/${data.run_id}/retry/${model.entry_id}`,{method:'POST'}))));card.append(retry);
        }
        out.append(card);
      }
    }
    clearTimeout(timer);
    if(data.run.status === 'running' && data.active_attempts.length)timer=setTimeout(()=>action(async()=>render(await api(`/api/file-jobs/${active}`))),1000);
    try {localStorage.setItem('p7-last-file-job',active);}catch{}
  }
  async function history() {
    const data=await api('/api/file-jobs'); el('start-paid-job').disabled=!data.paid_enabled;
    el('file-job-status').textContent=data.paid_enabled ? 'Maksas izpilde iespējota serverī.' : 'Maksas API izpilde serverī nav iespējota.';
    el('file-job-history').replaceChildren(new Option('Izvēlies izpildi',''));
    for(const id of data.runs)el('file-job-history').add(new Option(id,id));
  }
  async function start(mode) {
    const prep=el('audio-prepared');
    if(prep.hidden || !prep.dataset.preparationId)throw new Error('Vispirms sagatavo audio intervālu.');
    render(await api('/api/file-jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preparation_id:prep.dataset.preparationId,execution_mode:mode})}));
    await history();el('file-job-history').value=active;
  }
  el('start-demo-job').addEventListener('click',()=>action(()=>start('demo')));
  el('start-paid-job').addEventListener('click',()=>action(()=>start('paid')));
  el('open-file-job').addEventListener('click',()=>action(async()=>{const id=el('file-job-history').value;if(!id)throw new Error('Izvēlies izpildi.');render(await api(`/api/file-jobs/${id}`));}));
  action(async()=>{await history();let id;try{id=localStorage.getItem('p7-last-file-job');}catch{}if(id){render(await api(`/api/file-jobs/${id}`));el('file-job-history').value=id;}});
})();
