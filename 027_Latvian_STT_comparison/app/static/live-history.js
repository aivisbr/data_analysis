/* Read-only disk history. Returning to the page never resumes media capture. */
(() => {
  const el=id=>document.getElementById(id);
  const names={queued:'Gaida',running:'Izpildē',complete:'Pabeigts',incomplete:'Nepilnīgs',
    interrupted:'Pārtraukts',failed:'Kļūda',cancelled:'Atcelts',saved:'Saglabāts',pending:'Vēl nav saglabāts'};
  let selected=null,revision=0,busy=false;
  const text=(tag,value)=>{const node=document.createElement(tag);node.textContent=value;return node;};
  async function request(url) {
    const response=await fetch(url,{cache:'no-store'});
    if(!response.ok)throw new Error('Tiešraides vēsturi nevar nolasīt.');return response.json();
  }
  async function show(identity) {
    selected=identity;const generation=++revision;
    try {
      const data=await request(`/api/live-runs/${identity}`);
      if(generation!==revision)return;
      const option=Array.from(el('live-history-select').options).find(item=>item.value===identity);
      if(option)option.textContent=`${new Date(data.created_at).toLocaleString('lv-LV')} · ${names[data.status] || data.status} · ${identity}`;
      const container=el('live-history-results');container.replaceChildren();
      container.append(text('p',`${data.execution_mode==='demo'?'DEMO':'Maksas tiešraide'} · ${data.run_id} · ${names[data.status] || data.status}`));
      if(data.recovery_error)container.append(text('p',data.recovery_error));
      for(const attempt of data.attempts) {
        const section=document.createElement('section');section.className='input-card';
        section.append(text('h4',`${attempt.model} · ${attempt.reading==='A'?'Try1':'Try2'}`));
        section.append(text('p',`${names[attempt.status] || attempt.status} · ${names[attempt.save_status] || attempt.save_status}`));
        section.append(text('p',data.execution_mode==='demo'?'Apstiprinātais teksts (DEMO piemērs, nav audio transkripts):':'Apstiprinātais transkripts:'),text('pre',attempt.final_text || 'Nav apstiprināta teksta.'));
        if(attempt.provisional_text)section.append(text('p','Pagaidu teksts — netiek eksportēts:'),text('pre',attempt.provisional_text));
        for(const error of attempt.errors)section.append(text('p',error.message));
        if(attempt.save_error)section.append(text('p',attempt.save_error.message));
        for(const [kind,url] of Object.entries(attempt.files)) {
          const link=document.createElement('a');link.href=url;link.textContent=kind==='raw'?'Sākotnējais TXT':'Rediģējamais TXT';
          section.append(link,document.createTextNode(' '));
        }
        container.append(section);
      }
      el('live-history-status').textContent='Vēsture nolasīta no diska. Jaunam ierakstam atkārtoti spied Sākt un izvēlies audio cilni.';
    } catch(error) {if(generation===revision)el('live-history-status').textContent=error.message;}
  }
  async function refresh(preferred=selected) {
    try {
      const data=await request('/api/live-runs');const select=el('live-history-select');select.replaceChildren();
      for(const run of data.runs) {
        const option=document.createElement('option');option.value=run.run_id;
        option.textContent=`${new Date(run.created_at).toLocaleString('lv-LV')} · ${names[run.status] || run.status} · ${run.run_id}`;select.append(option);
      }
      if(preferred && data.runs.some(run=>run.run_id===preferred))select.value=preferred;
      if(select.value)await show(select.value);
      else el('live-history-status').textContent='Serverī saglabātu tiešraides izpilžu vēl nav. Pirms Sākt atzīmē Bezmaksas DEMO. Bez DEMO vietējie WAV ieraksti vēsturē netiek saglabāti.';
      if(Object.keys(data.recovery_errors).length)el('live-history-status').textContent='Dažu izpilžu atkopšana vai nolasīšana neizdevās. Saglabātos rezultātus skatiet zemāk.';
    } catch(error) {el('live-history-status').textContent=error.message;}
  }
  el('live-history-refresh').addEventListener('click',()=>refresh());
  el('live-history-select').addEventListener('change',event=>show(event.target.value));
  document.addEventListener('dictation:live-run',event=>refresh(event.detail.run_id));
  setInterval(async()=>{if(selected && !busy && !document.hidden){busy=true;try{await show(selected);}finally{busy=false;}}},2000);
  refresh();
})();
