/* Source-rate interleaved PCM16. Video never enters this processor. */
class DictationPCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.active=false; this.sequence=0; this.accepted=0; this.channels=0; this.used=0;
    this.capacity=Math.round(sampleRate*.02); this.buffer=null; this.segment=null;
    this.port.onmessage=({data})=>{
      if(data.action==='prepare') {
        this.prepareRequest=data.request_id;this.stableSamples=0;this.probeEnd=null;this.probeChannels=null;return;
      }
      if(data.action==='start') {this.segment=data.segment_id;this.active=true;}
      if(data.action==='pause' || data.action==='stop') {
        this.flush();this.active=false;
      }
      this.port.postMessage({type:'ack',request_id:data.request_id,source_sample:currentFrame,accepted_samples:this.accepted});
    };
  }
  flush() {
    if(!this.used)return;
    const pcm=this.buffer.slice(0,this.used*this.channels*2);
    this.port.postMessage({type:'frame',sequence:this.sequence++,segment_id:this.segment,
      sample_rate:sampleRate,channels:this.channels,source_start_sample:this.begin,
      source_end_sample:this.begin+this.used,audio_start_sample:this.accepted,
      audio_end_sample:this.accepted+this.used,frame_samples:this.used,
      rms:Math.sqrt(this.energy/(this.used*this.channels)),peak:this.peak,missing_input_samples:this.missingInput,pcm},[pcm]);
    this.accepted+=this.used;this.used=0;this.energy=0;this.peak=0;this.missingInput=0;
  }
  process(inputs,outputs) {
    for(const channel of outputs[0] || [])channel.fill(0); // No local monitoring/echo.
    const channels=inputs[0] || [];
    const length=channels[0]?.length || outputs[0]?.[0]?.length || 0;
    if(this.prepareRequest && channels.length) {
      // Startup rendering can precede a steadily advancing AudioContext clock.
      // Require 20 ms of contiguous observed quanta before admitting source audio.
      this.stableSamples=this.probeEnd===currentFrame && this.probeChannels===channels.length?this.stableSamples+length:0;
      this.probeEnd=currentFrame+length;this.probeChannels=channels.length;
      if(this.stableSamples>=this.capacity) {
        this.preparedChannels=channels.length;
        this.port.postMessage({type:'ack',request_id:this.prepareRequest,source_sample:currentFrame,
          sample_rate:sampleRate,channels:channels.length,accepted_samples:this.accepted});
        this.prepareRequest=null;
      }
    }
    if(!this.active || (!channels.length && !this.channels && !this.preparedChannels))return true;
    if(channels.length && this.channels && this.channels!==channels.length) {
      this.flush();this.active=false;this.port.postMessage({type:'error',code:'channel_change'});return true;
    }
    if(!this.channels) {
      this.channels=channels.length || this.preparedChannels;this.buffer=new ArrayBuffer(this.capacity*this.channels*2);
      this.view=new DataView(this.buffer);this.energy=0;this.peak=0;this.missingInput=0;
    }
    // Derive quantum size from input, not an assumed fixed 128 samples.
    for(let i=0;i<length;i++) {
      if(!this.used)this.begin=currentFrame+i;
      if(!channels.length)this.missingInput++;
      for(let c=0;c<this.channels;c++) {
        const value=Math.max(-1,Math.min(1,channels[c]?.[i] || 0));
        this.energy+=value*value;this.peak=Math.max(this.peak,Math.abs(value));
        this.view.setInt16((this.used*this.channels+c)*2,Math.round(value<0?value*32768:value*32767),true);
      }
      this.used++;
      if(this.used===this.capacity)this.flush();
    }
    return true;
  }
}
registerProcessor('dictation-pcm',DictationPCMProcessor);
