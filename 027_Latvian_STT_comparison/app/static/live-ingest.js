/* Explicit local-server DEMO/paid transport; no SDKs, keys or provider endpoints in the browser. */
(() => {
  class LiveIngest {
    constructor(onFailure,mode='demo') {this.mode=mode;this.onFailure=onFailure;this.pending=null;this.socket=null;this.result=null;this.closed=false;}
    async connect() {
      const url=new URL('/api/live-ingest',location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
      this.socket=new WebSocket(url);
      await new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{this.close();reject(new Error('Savienojums ar vietējo serveri nav gatavs.'));},10000);
        this.socket.onopen=()=>{clearTimeout(timer);resolve();};
        this.socket.onerror=()=>{clearTimeout(timer);reject(new Error('Nevar savienoties ar vietējo serveri.'));};
        this.socket.onclose=()=>{clearTimeout(timer);reject(new Error('Vietējais savienojums pārtrūka.'));};
      });
      this.socket.onmessage=event=>{
        try {
          const message=JSON.parse(event.data);
          if(message.type==='error')throw new Error(message.message);
          if(!this.pending || message.type!==this.pending.expected)throw new Error('Negaidīta servera atbilde.');
          this.result=message;this.pending.resolve(message);
        } catch(error) {this.fail(error);}
      };
      this.socket.onerror=()=>this.fail(new Error('Vietējais audio savienojums pārtrūka.'));
      this.socket.onclose=()=>{if(!this.closed)this.fail(new Error('Vietējais audio savienojums aizvērts.'));};
    }
    fail(error) {
      if(this.pending)this.pending.reject(error);
      if(!this.closed){this.close();this.onFailure(error.message);}
    }
    request(message,expected) {
      if(this.pending || this.socket?.readyState!==WebSocket.OPEN)return Promise.reject(new Error('Audio savienojums nav gatavs.'));
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>this.fail(new Error('Servera apstiprinājums nav saņemts.')),this.mode==='paid'?210000:30000);
        const finish=callback=>value=>{clearTimeout(timer);this.pending=null;callback(value);};
        this.pending={expected,resolve:finish(resolve),reject:finish(reject)};
        try{this.socket.send(JSON.stringify(message));}catch(error){this.fail(error);}
      });
    }
    start(source) {return this.request({type:'start',source,execution_mode:this.mode},'ready');}
    resume(source) {return this.request({type:'resume',source},'ready');}
    pause() {return this.request({type:'pause'},'paused');}
    async stop(outcome={}) {try{return await this.request({type:'stop',...outcome},'stopped');}finally{this.close();}}
    frame(data) {
      if(this.closed || this.pending || this.socket?.readyState!==WebSocket.OPEN)throw new Error('Audio savienojums nav gatavs kadriem.');
      if(this.socket.bufferedAmount+data.pcm.byteLength>1024*1024)throw new Error('Audio sūtīšanas rinda pārpildīta; ieraksts ir nepilnīgs.');
      const {sequence,segment_id,sample_rate,channels,frame_samples,source_start_sample,source_end_sample,audio_start_sample,audio_end_sample,missing_input_samples}=data;
      this.socket.send(JSON.stringify({type:'frame',header:{sequence,segment_id,sample_rate,channels,frame_samples,
        source_start_sample,source_end_sample,audio_start_sample,audio_end_sample,missing_input_samples}}));
      this.socket.send(data.pcm);
    }
    close() {
      this.closed=true;
      if(this.pending)this.pending.reject(new Error('Vietējais audio savienojums aizvērts.'));
      this.socket?.close();
    }
  }
  window.DictationLiveIngest=LiveIngest;
})();
