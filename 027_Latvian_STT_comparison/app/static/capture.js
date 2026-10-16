/* Local capture plus explicit DEMO/paid backend mode; user owns browser testing. */
(() => {
  const el=id=>document.getElementById(id);
  let stream,context,source,worklet,silent,recording,session,state='idle',stopping=null,urls=[];
  let ingest=null,demo=false,paid=false,paidEnabled=false;
  const pending=new Map();let requestId=0;
  el('capture-paid').addEventListener('change',()=>{if(el('capture-paid').checked)el('capture-demo').checked=false;});
  el('capture-demo').addEventListener('change',()=>{if(el('capture-demo').checked)el('capture-paid').checked=false;});
  fetch('/api/live-config').then(response=>{if(!response.ok)throw new Error();return response.json();}).then(config=>{
    paidEnabled=config.paid_enabled;el('capture-paid').disabled=!paidEnabled || !['idle','stopped'].includes(state);
    el('capture-paid-help').textContent=paidEnabled?'Maksas tiešraide iespējota. Ieslēdz tikai vajadzīgos modeļus; pārbaudi budžetu. ElevenLabs: '+(config.elevenlabs_enabled?'iespējots.':'bloķēts.'):'Maksas tiešraide serverī nav iespējota. Failu transkripcijas atļauja to neieslēdz.';
  }).catch(()=>{el('capture-paid-help').textContent='Nevar nolasīt tiešraides atļaujas; maksas režīms bloķēts.';});
  const states={idle:'Nav sākts',preparing:'Sagatavo audio',ready:'Gatavs',recording:'Ieraksta',pausing:'Pauzē',paused:'Pauze — audio izslēgts',stopping:'Noslēdz ierakstu',stopped:'Apturēts'};
  const tell=text=>{el('capture-status').textContent=text;};
  function update(next) {
    state=next;el('capture-state').textContent=states[state];
    el('capture-start').disabled=!['idle','stopped'].includes(state);
    el('capture-pause').disabled=state!=='recording';el('capture-resume').disabled=state!=='paused';
    el('capture-stop').disabled=!['recording','paused'].includes(state);
    el('capture-reading').disabled=!['idle','stopped'].includes(state);
    el('capture-demo').disabled=!['idle','stopped'].includes(state);
    el('capture-paid').disabled=!paidEnabled || !['idle','stopped'].includes(state);
  }
  function command(action,segmentId=crypto.randomUUID()) {
    const id=++requestId;
    return new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>{pending.delete(id);reject(new Error('worklet_timeout'));},2000);
      pending.set(id,{resolve:data=>{clearTimeout(timeout);resolve(data);}});
      worklet.port.postMessage({action,request_id:id,segment_id:segmentId});
    });
  }
  function packet({data}) {
    if(data.type==='ack') {pending.get(data.request_id)?.resolve(data);pending.delete(data.request_id);return;}
    if(data.type==='error') {stop('Mainījās avota kanālu skaits. Ieraksts ir nepilnīgs.');return;}
    if(data.type!=='frame')return;
    try {
      recording.append(data);
      if(ingest)ingest.frame(data);
      el('capture-level').value=Math.min(1,data.rms*4);
      el('capture-stats').textContent=`${recording.rate} Hz · ${recording.channels} kanāli · ${recording.frames.length} kadri · ${(recording.samples/recording.rate).toLocaleString('lv-LV',{maximumFractionDigits:1})} s`;
      // Optional subscribers receive source PCM only.
      document.dispatchEvent(new CustomEvent('dictation:pcm',{detail:{session_id:session,...data}}));
    } catch(error) {
      const previous=recording.frames[recording.frames.length-1];
      recording.failure ||= {code:error.message,expected_sequence:recording.frames.length,
        actual_sequence:data.sequence,expected_source_sample:previous?.source_end_sample,
        actual_source_sample:data.source_start_sample,expected_audio_sample:recording.samples,
        actual_audio_sample:data.audio_start_sample,channels:data.channels,sample_rate:data.sample_rate};
      stop(error.message==='recording_limit'?'Sasniegts vietējā ieraksta limits (128 MiB vai 30 minūtes). Ieraksts ir nepilnīgs.':
        ['source_gap','frame_order'].includes(error.message)?'Audio kadru secībā ir atstarpe vai kļūda. Ieraksts ir nepilnīgs. Diagnostika pievienota kadru metadatiem.':error.message);
    }
  }
  function backendStatus(result) {
    const models=Object.values(result.models);
    const failed=models.filter(model=>model.error || model.status==='storage_failed').length;
    el('capture-backend').textContent=`${result.execution_mode==='demo'?'DEMO':'Maksas tiešraide'} · izpilde ${result.run_id} · ${failed} modeļi ar kļūdu no ${models.length} · ${states[result.state] || result.state}. ${result.execution_mode==='demo'?'DEMO teksts nav audio transkripts.':''}`;
    document.dispatchEvent(new CustomEvent('dictation:live-run',{detail:{run_id:result.run_id}}));
  }
  async function startSegment(resume=false) {
    // Probe actual input channels while inactive; freeze the sample clock during readiness.
    const format=await command('prepare');
    if(!ingest){await command('start');return;}
    await context.suspend();
    const segmentId=crypto.randomUUID();
    const info={reading:recording.reading,segment_id:segmentId,sample_rate:format.sample_rate,
      channels:format.channels,source_start_sample:Math.round(context.currentTime*format.sample_rate)};
    const result=await (resume?ingest.resume(info):ingest.start(info));backendStatus(result);
    const started=command('start',segmentId);await context.resume();await started;
  }
  function exportRecording(reason) {
    el('capture-downloads').replaceChildren();
    if(!recording?.frames.length){tell(reason || 'Audio kadri nav saņemti. Pārbaudi, vai atlasītajā cilnē tiek atskaņots audio.');return;}
    const metadata={...recording.metadata(),session_id:session,status:reason?'incomplete':'stopped',reason:reason || null,
      capture_error:recording.failure || null,
      source_track_settings:stream?.getAudioTracks()[0]?.getSettings() || {},clock:'AudioContext sample clock; absolute source versus accepted samples'};
    const prefix=`diktats_${metadata.directory}_${session}`;
    for(const [suffix,blob,label] of [
      ['wav',new Blob(recording.wavParts(),{type:'audio/wav'}),'Lejupielādēt WAV'],
      ['json',new Blob([JSON.stringify(metadata,null,2)],{type:'application/json'}),'Lejupielādēt kadru metadatus']]) {
      const url=URL.createObjectURL(blob);urls.push(url);const link=document.createElement('a');
      link.href=url;link.download=`${prefix}.${suffix}`;link.textContent=label;el('capture-downloads').append(link);
    }
    tell(reason || (paid?'Audio un modeļu apstiprinātie transkripti saglabāti serverī. Pārbaudi katra modeļa statusu vēsturē.':demo?'Audio saglabāts vietējā DEMO izpildē un sagatavots lejupielādei. Īstie modeļi netika izsaukti.':'Audio ieraksts sagatavots lejupielādei. Tas paliek pārlūkā līdz saglabāšanai; modeļiem audio netika nosūtīts.'));
  }
  async function release() {
    stream?.getTracks().forEach(track=>track.stop());
    for(const node of [source,worklet,silent]) {try{node?.disconnect();}catch{}}
    if(context && context.state!=='closed') {try{await context.close();}catch{}}
    stream=context=source=worklet=silent=null;
  }
  async function stop(reason=null) {
    if(stopping)return stopping;
    if(!worklet || ['idle','stopped'].includes(state))return;
    update('stopping');
    stopping=(async()=>{
      try {await command('stop');}catch{reason=reason || 'Audio noslēgšanas apstiprinājums nav saņemts. Ieraksts ir nepilnīgs.';}
      if(ingest) {
        try {
          const result=await ingest.stop({interrupted:Boolean(reason),expected_frames:recording.frames.length,
            expected_samples:recording.samples});backendStatus(result);
          if(result.error || result.archive_status==='failed')reason=reason || result.error?.message || 'Avota audio saglabāšana serverī neizdevās.';
        }catch(error){reason=reason || error.message;}
        ingest.close();ingest=null;
      }
      exportRecording(reason);
      try {await release();}finally{el('capture-level').value=0;update('stopped');stopping=null;}
    })();
    return stopping;
  }
  el('capture-start').addEventListener('click',async()=>{
    if(!['idle','stopped'].includes(state))return;
    if(!window.isSecureContext || !navigator.mediaDevices?.getDisplayMedia || !window.AudioWorkletNode) {
      tell('Šeit cilnes audio uztveršana nav atbalstīta. Atver http://127.0.0.1:8000 pārlūkā Chrome vai Edge.');return;
    }
    update('preparing');tell('Izvēlies diktāta cilni un ieslēdz cilnes audio kopīgošanu.');
    try {
      // Call directly from the click activation; video is mandatory for the picker.
      stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},
        selfBrowserSurface:'exclude',systemAudio:'exclude',surfaceSwitching:'exclude'});
      if(!stream.getAudioTracks().some(track=>track.readyState==='live'))throw new Error('missing_audio');
      const surface=stream.getVideoTracks()[0]?.getSettings().displaySurface;
      if(surface && surface!=='browser')throw new Error('not_tab');
      const rate=stream.getAudioTracks()[0].getSettings().sampleRate;
      context=new AudioContext(rate?{sampleRate:rate}:{});
      await context.audioWorklet.addModule('/static/pcm-capture-worklet.js?v=T019b');
      recording=new DictationCapture.PCMRecording(el('capture-reading').value);session=crypto.randomUUID();
      source=context.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
      worklet=new AudioWorkletNode(context,'dictation-pcm',{numberOfInputs:1,numberOfOutputs:1,channelCountMode:'max'});
      worklet.port.onmessage=packet;worklet.onprocessorerror=()=>stop('Audio apstrāde pārtrūka. Ieraksts ir nepilnīgs.');
      silent=context.createGain();silent.gain.value=0;source.connect(worklet);worklet.connect(silent);silent.connect(context.destination);
      for(const track of stream.getTracks())track.addEventListener('ended',()=>stop('Cilnes kopīgošana pārtraukta. Saglabāti saņemtie audio kadri.'),{once:true});
      demo=el('capture-demo').checked;
      paid=el('capture-paid').checked;
      el('capture-backend').textContent=paid?'Maksas tiešraide: gaida modeļu gatavību…':demo?'DEMO: gaida vietējā servera gatavību…':'Vietējais ieraksts: audio serverim un modeļiem netiek sūtīts.';
      if(demo || paid){ingest=new DictationLiveIngest(message=>stop(message),paid?'paid':'demo');await ingest.connect();}
      await context.resume();await startSegment();
      urls.forEach(url=>URL.revokeObjectURL(url));urls=[];el('capture-downloads').replaceChildren();
      el('capture-stats').textContent='Gaida pirmos audio kadrus…';update('recording');tell('Ieraksta tikai cilnes audio. Video netiek saglabāts vai nosūtīts.');
    } catch(error) {
      if(ingest){ingest.close();ingest=null;}
      await release();update('idle');
      const message={missing_audio:'Nav audio celiņa. Izvēlies pārlūka cilni un atzīmē audio kopīgošanu.',
        not_tab:'Izvēlies pārlūka cilni, nevis logu vai visu ekrānu.',NotAllowedError:'Kopīgošana atcelta vai liegta. Spied Sākt un izvēlies cilni ar audio.',
        NotReadableError:'Pārlūks nevar nolasīt cilnes audio. Pārbaudi avotu un mēģini vēlreiz.'};
      tell(message[error.message] || message[error.name] || 'Audio uztveršanu nevar sākt. Izmanto Chrome/Edge un cilni ar audio.');
    }
  });
  el('capture-pause').addEventListener('click',async()=>{
    if(state!=='recording')return;update('pausing');
    try {await command('pause');if(ingest)backendStatus(await ingest.pause());el('capture-level').value=0;update('paused');tell('Pauze: atskaņotais audio netiek pievienots ierakstam.');}
    catch{await stop('Pauzes apstiprinājums nav saņemts. Ieraksts ir nepilnīgs.');}
  });
  el('capture-resume').addEventListener('click',async()=>{
    if(state!=='paused')return;update('preparing');
    try {await startSegment(true);update('recording');tell('Ieraksts turpinās jaunā segmentā. Pauzes audio netiek atkārtots.');}
    catch{await stop('Audio nevar turpināt. Ieraksts ir nepilnīgs.');}
  });
  el('capture-stop').addEventListener('click',()=>stop());
  window.addEventListener('beforeunload',event=>{if(recording?.frames.length && !['idle'].includes(state)){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pagehide',()=>{ingest?.close();stream?.getTracks().forEach(track=>track.stop());});
  window.addEventListener('pageshow',async event=>{
    if(!event.persisted)return;
    ingest?.close();ingest=null;
    exportRecording('Lapa atjaunota: iepriekšējā uztveršana pārtraukta. Jaunam ierakstam spied Sākt un atkārtoti izvēlies audio cilni.');
    await release();el('capture-level').value=0;update('stopped');
  });
  update('idle');
})();
