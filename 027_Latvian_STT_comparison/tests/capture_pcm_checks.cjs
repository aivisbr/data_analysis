// Offline DSP checks only: no DOM, browser, media-device or web-app automation.
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
require('../app/static/capture-core.js');
const {PCMRecording}=global.DictationCapture;
const messages=[];let Processor;
const sandbox={sampleRate:1000,currentFrame:0,AudioWorkletProcessor:class{constructor(){this.port={postMessage:m=>messages.push(m)};}},
  registerProcessor:(_name,type)=>Processor=type,ArrayBuffer,DataView,Math};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync('app/static/pcm-capture-worklet.js','utf8'),sandbox);
const processor=new Processor();
const command=(action,segment_id='one')=>processor.port.onmessage({data:{action,segment_id,request_id:1}});
const quantum=(values)=>{processor.process([values.map(v=>Float32Array.from(v))],[[new Float32Array(values[0].length)]]);sandbox.currentFrame+=values[0].length;};
command('start');quantum([[-1,0,.5,1],[1,.5,0,-1]]);command('pause');
const frame=messages.find(m=>m.type==='frame');assert.equal(frame.channels,2);assert.equal(frame.frame_samples,4);
assert.equal(new DataView(frame.pcm).getInt16(0,true),-32768);assert.equal(new DataView(frame.pcm).getInt16(2,true),32767);
assert.equal(frame.sequence,0);assert.equal(frame.source_start_sample,0);assert.equal(frame.audio_end_sample,4);
quantum([[1,1,1],[1,1,1]]);assert.equal(messages.filter(m=>m.type==='frame').length,1);
command('start','two');quantum([[.25,.25],[.25,.25]]);command('stop');
const frames=messages.filter(m=>m.type==='frame');assert.equal(frames[1].source_start_sample,7);assert.equal(frames[1].audio_start_sample,4);
assert.equal(frames[1].sequence,1);assert.equal(frames[1].segment_id,'two');
const recording=new PCMRecording('D');frames.forEach(f=>recording.append(f));
assert.equal(recording.samples,6);assert.equal(recording.metadata().directory,'Try2');
const parts=recording.wavParts(),wav=Buffer.concat(parts.map(p=>Buffer.from(p)));
assert.equal(wav.toString('ascii',0,4),'RIFF');assert.equal(wav.readUInt16LE(22),2);assert.equal(wav.readUInt32LE(24),1000);
assert.equal(wav.readUInt32LE(40),24);assert.equal(wav.length,68);assert.equal(wav.readInt16LE(44),-32768);
assert.throws(()=>recording.append(frames[1]),/frame_order/);
assert.throws(()=>new PCMRecording('A',{maxBytes:1}).append(frame),/recording_limit/);
assert.throws(()=>new PCMRecording('A').wavParts(),/empty/);
const gap=new PCMRecording('A');gap.append(frame);
assert.throws(()=>gap.append({...frames[1],segment_id:'one'}),/source_gap/);
assert.throws(()=>gap.append({...frames[1],audio_start_sample:99}),/frame_order/);
// Partial buffers and >128 render quanta: full 20 ms packets plus trailing flush.
messages.length=0;const p2=new Processor();p2.port.onmessage({data:{action:'start',segment_id:'three'}});
sandbox.currentFrame=100;p2.process([[new Float32Array(47).fill(.5)]],[[new Float32Array(47)]]);
sandbox.currentFrame=147;p2.port.onmessage({data:{action:'stop'}});
assert.deepEqual(messages.filter(m=>m.type==='frame').map(m=>m.frame_samples),[20,20,7]);
assert.deepEqual(messages.filter(m=>m.type==='frame').map(m=>m.source_start_sample),[100,120,140]);
// Readiness probe observes format without accepting audio or advancing accepted counters.
messages.length=0;const p3=new Processor();
p3.port.onmessage({data:{action:'prepare',request_id:42}});
sandbox.currentFrame=200;p3.process([[new Float32Array(8),new Float32Array(8)]],[[new Float32Array(8)]]);
assert.equal(messages.length,0);
for(const start of [208,216,224]) {sandbox.currentFrame=start;p3.process([[new Float32Array(8),new Float32Array(8)]],[[new Float32Array(8)]]);}
assert.equal(messages.length,1);assert.equal(messages[0].type,'ack');assert.equal(messages[0].request_id,42);
assert.equal(messages[0].channels,2);assert.equal(messages[0].source_sample,224);assert.equal(p3.accepted,0);
assert.equal(p3.active,false);
// A live source can temporarily supply no channel arrays. Those render quanta
// still consume source-clock time and must not create a gap in accepted PCM.
messages.length=0;const p4=new Processor();sandbox.sampleRate=1000;
p4.port.onmessage({data:{action:'start',segment_id:'missing-input'}});
for(let index=0;index<3;index++) {
  sandbox.currentFrame=300+index*20;
  p4.process(index===1?[]:[[new Float32Array(20).fill(.5)]],[[new Float32Array(20)]]);
}
p4.port.onmessage({data:{action:'stop'}});
const missingFrames=messages.filter(m=>m.type==='frame');
const missingRecording=new PCMRecording('A');missingFrames.forEach(frame=>missingRecording.append(frame));
assert.equal(missingRecording.samples,60);
assert.equal(missingFrames[1].missing_input_samples,20);
assert.ok(new Int16Array(missingFrames[1].pcm).every(sample=>sample===0));
// Realistic stereo 48 kHz/128-sample render quanta cross 960-sample packet boundaries.
messages.length=0;sandbox.sampleRate=48000;const p5=new Processor();
p5.port.onmessage({data:{action:'start',segment_id:'real-rate'}});
for(let index=0;index<24;index++) {
  sandbox.currentFrame=1000+index*128;
  p5.process(index===8?[]:[[new Float32Array(128).fill(.5),new Float32Array(128).fill(.25)]],
    [[new Float32Array(128),new Float32Array(128)]]);
}
p5.port.onmessage({data:{action:'stop'}});
const realisticRecording=new PCMRecording('A');messages.filter(m=>m.type==='frame').forEach(frame=>realisticRecording.append(frame));
assert.equal(realisticRecording.samples,3072);assert.equal(realisticRecording.frames.length,4);
assert.equal(realisticRecording.frames.reduce((count,frame)=>count+frame.missing_input_samples,0),128);
// DEMO probing also supplies channel identity for silent quanta before the first packet.
messages.length=0;const p6=new Processor();
p6.port.onmessage({data:{action:'prepare',request_id:9}});
sandbox.currentFrame=5000;p6.process([[new Float32Array(128)]],[[new Float32Array(128)]]);
for(let index=1;index<=8;index++) {sandbox.currentFrame=5000+index*128;p6.process([[new Float32Array(128)]],[[new Float32Array(128)]]);}
p6.port.onmessage({data:{action:'start',segment_id:'probe-then-empty'}});
sandbox.currentFrame=6152;p6.process([],[[new Float32Array(128)]]);
p6.port.onmessage({data:{action:'stop'}});
assert.equal(messages.find(m=>m.type==='frame').source_start_sample,6152);
assert.equal(messages.find(m=>m.type==='frame').missing_input_samples,128);
// Replay the supplied failed-log clock pattern: zero-time warmup then jump.
for(const firstRealClock of [1024,2432]) {
  // Without preparation, a first quantum at zero followed by a clock jump
  // yields exactly the supplied logs' second packet starts: 1984 or 3392.
  messages.length=0;const premature=new Processor();
  premature.port.onmessage({data:{action:'start',segment_id:'premature'}});
  for(let index=0;index<16;index++) {
    sandbox.currentFrame=index===0?0:firstRealClock+index*128;
    premature.process([[new Float32Array(128),new Float32Array(128)]],[[new Float32Array(128)]]);
  }
  const prematureFrames=messages.filter(m=>m.type==='frame');
  assert.equal(prematureFrames[0].source_start_sample,0);
  assert.equal(prematureFrames[1].source_start_sample,firstRealClock+960);
  const rejected=new PCMRecording('A');rejected.append(prematureFrames[0]);
  assert.throws(()=>rejected.append(prematureFrames[1]),/source_gap/);
  messages.length=0;const startup=new Processor();startup.port.onmessage({data:{action:'prepare',request_id:77}});
  const render=start=>{sandbox.currentFrame=start;startup.process([[new Float32Array(128),new Float32Array(128)]],[[new Float32Array(128)]]);};
  for(let index=0;index<8;index++)render(0);
  assert.equal(messages.length,0);assert.equal(startup.accepted,0);
  for(let index=0;index<=8;index++)render(firstRealClock+index*128);
  assert.equal(messages.filter(m=>m.type==='ack').length,1);
  startup.port.onmessage({data:{action:'start',segment_id:'stable-start'}});
  for(let index=9;index<33;index++)render(firstRealClock+index*128);
  startup.port.onmessage({data:{action:'stop'}});
  const valid=new PCMRecording('A');messages.filter(m=>m.type==='frame').forEach(frame=>valid.append(frame));
  assert.equal(valid.samples,3072);assert.equal(valid.frames.length,4);
}
console.log('PASS: PCM encoding, pause exclusion, stable startup clocks, 48 kHz boundaries, missing-input silence, timestamps, WAV, gaps and limits.');
