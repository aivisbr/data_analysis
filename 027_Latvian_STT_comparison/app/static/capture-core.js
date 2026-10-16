/* Pure audio bookkeeping/encoding; no DOM, browser permissions or API requests. */
((root)=>{
  class PCMRecording {
    constructor(reading,{maxBytes=128*1024*1024,maxSeconds=1800}={}) {
      this.reading=reading;this.maxBytes=maxBytes;this.maxSeconds=maxSeconds;
      this.frames=[];this.bytes=0;this.samples=0;this.rate=0;this.channels=0;
    }
    append(frame) {
      if(frame.sequence!==this.frames.length || !Number.isInteger(frame.frame_samples) || frame.frame_samples<=0 ||
          !Number.isInteger(frame.sample_rate) || frame.sample_rate<=0 || !Number.isInteger(frame.channels) || frame.channels<1 || frame.channels>8 ||
          !(frame.pcm instanceof ArrayBuffer) || frame.pcm.byteLength!==frame.frame_samples*frame.channels*2 ||
          frame.audio_start_sample!==this.samples || frame.audio_end_sample!==this.samples+frame.frame_samples ||
          !Number.isSafeInteger(frame.source_start_sample) || frame.source_start_sample<0 ||
          frame.source_end_sample!==frame.source_start_sample+frame.frame_samples || !frame.segment_id ||
          (this.rate && (frame.sample_rate!==this.rate || frame.channels!==this.channels)))throw new Error('frame_order');
      const previous=this.frames[this.frames.length-1];
      if(previous && (frame.source_start_sample<previous.source_end_sample ||
          (previous.segment_id===frame.segment_id && frame.source_start_sample!==previous.source_end_sample)))throw new Error('source_gap');
      if(this.bytes+frame.pcm.byteLength>this.maxBytes || frame.audio_end_sample/frame.sample_rate>this.maxSeconds)throw new Error('recording_limit');
      this.rate=frame.sample_rate;this.channels=frame.channels;this.samples=frame.audio_end_sample;
      this.bytes+=frame.pcm.byteLength;this.frames.push(frame);
    }
    wavParts() {
      if(!this.frames.length)throw new Error('empty');
      const header=new ArrayBuffer(44),v=new DataView(header);
      const ascii=(at,text)=>{for(let i=0;i<text.length;i++)v.setUint8(at+i,text.charCodeAt(i));};
      ascii(0,'RIFF');v.setUint32(4,36+this.bytes,true);ascii(8,'WAVE');ascii(12,'fmt ');
      v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,this.channels,true);
      v.setUint32(24,this.rate,true);v.setUint32(28,this.rate*this.channels*2,true);
      v.setUint16(32,this.channels*2,true);v.setUint16(34,16,true);ascii(36,'data');v.setUint32(40,this.bytes,true);
      return [header,...this.frames.map(f=>f.pcm)];
    }
    metadata() {
      return {schema_version:1,reading:this.reading,directory:this.reading==='A'?'Try1':'Try2',
        codec:'pcm_s16le',sample_rate:this.rate,channels:this.channels,accepted_samples:this.samples,
        captured_seconds:this.rate?this.samples/this.rate:0,bytes:this.bytes,
        frames:this.frames.map(({pcm,...metadata})=>metadata)};
    }
  }
  root.DictationCapture={PCMRecording};
})(typeof globalThis!=='undefined'?globalThis:this);
