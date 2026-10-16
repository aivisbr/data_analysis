"""Dedicated Gemini live transcription. SDK imports/clients are lazy and gated."""
import asyncio
import math
import os
import time

from app.contracts import Provider, Mode, ErrorCode, ErrorDetails, PartialTranscriptEvent, FinalTranscriptEvent, ErrorEvent
from app.providers.base import ProviderFailure
from app.providers.settings import effective_settings
from app.providers.gemini_file import safe_error

MODEL='gemini-3.5-transcribe-live'


def failure(code,message):return ProviderFailure(ErrorDetails(code=code,message=message))


def live_config(request):
    model=request.model;settings=model.effective_settings
    if (model.provider!=Provider.GEMINI or model.mode!=Mode.LIVE or model.model_id!=MODEL or
        model.capability_profile_id!=f'gemini:{MODEL}:live'):
        raise failure(ErrorCode.UNAVAILABLE_MODEL,'Šis adapteris atbalsta tikai gemini-3.5-transcribe-live.')
    try:resolved=effective_settings(Provider.GEMINI,model.requested_settings)
    except ValueError:raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'Gemini valodas iestatījums nav derīgs.') from None
    if (settings!=resolved or model.capability_profile_version!=1 or settings.prompt is not None or
        settings.latency is not None or len(settings.keywords)>1000):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'Gemini tiešraides iestatījumi nav saderīgi.')
    return {'response_modalities':['TEXT'],
        'input_audio_transcription':{'language_codes':[settings.language_hint] if settings.language_hint else [],
            'custom_vocabulary':list(settings.keywords),
            'mode':'SMART' if settings.formatting=='smart' else 'VERBATIM'},
        'realtime_input_config':{'automatic_activity_detection':{'disabled':settings.turn_detection=='manual'}}}


class GeminiLiveAdapter:
    def __init__(self, *, timeout_seconds=10, final_timeout_seconds=None, client_factory=None, allow_paid=False):
        if not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=120:raise ValueError('Invalid timeout')
        self.timeout=timeout_seconds;self.factory=client_factory;self.allow_paid=allow_paid
        self.final_timeout=timeout_seconds if final_timeout_seconds is None else final_timeout_seconds
        if not math.isfinite(self.final_timeout) or not 0<self.final_timeout<=120:raise ValueError('Invalid final timeout')

    async def open_live(self,request):
        config=live_config(request)
        if not self.allow_paid:raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'Maksas Gemini tiešraides izpilde nav iespējota.')
        key=os.environ.get('GEMINI_API_KEY','').strip()
        if not key:raise failure(ErrorCode.AUTHENTICATION,'Nav konfigurēta GEMINI_API_KEY atslēga.')
        try:
            from google import genai
            from google.genai import types
        except ImportError:raise failure(ErrorCode.INTERNAL,'google-genai Python pakotne nav pieejama.') from None
        client=None;manager=None;session=None
        try:
            client=(self.factory or genai.Client)(api_key=key,vertexai=False,http_options=types.HttpOptions(
                base_url='https://generativelanguage.googleapis.com',api_version='v1beta',
                timeout=round(self.timeout*1000),retry_options=types.HttpRetryOptions(attempts=1)))
            manager=client.aio.live.connect(model=MODEL,config=types.LiveConnectConfig(**config))
            async with asyncio.timeout(self.timeout):raw=await manager.__aenter__()
            session=GeminiLiveSession(request,config,client,manager,raw,types,self.timeout)
            session.final_timeout=self.final_timeout
            if not raw.setup_complete:raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'Gemini neapstiprināja tiešraides sesijas izveidi.')
            session.reader=asyncio.create_task(session._read())
            return session
        except BaseException as error:
            if session:
                try:await session.close()
                except Exception:pass
            elif client:
                for operation in (client.aio.aclose,lambda:asyncio.to_thread(client.close)):
                    try:
                        async with asyncio.timeout(self.timeout):await operation()
                    except Exception:pass
            if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):raise
            raise error if isinstance(error,ProviderFailure) else safe_error(error) from None


class GeminiLiveSession:
    def __init__(self,request,config,client,manager,raw,types,timeout):
        self.request=request;self.config=config;self.client=client;self.manager=manager;self.raw=raw;self.types=types;self.timeout=timeout
        self.queue=asyncio.Queue(256);self.lock=asyncio.Lock();self.changed=asyncio.Event();self.buffer=bytearray()
        self.reader=None;self.closed=False;self.flushing=False;self.finished=False;self.error=None
        self.manual=request.model.effective_settings.turn_detection=='manual';self.activity=False
        self.sequence=-1;self.offset=-1;self.samples=0;self.sent_samples=0;self.started=time.monotonic()
        self.index=0;self.current=None;self.spans={};self.offsets={};self.final_count=0;self.turn_after_end=False
        self.flush_final_count=0;self.flush_finished_transcription=False
        self.end_sent=False
        self.final_timeout=timeout;self.boundary_type=None;self.message_count=0;self.post_end_finals=0

    async def _send(self,**kwargs):
        try:
            async with asyncio.timeout(self.timeout):await self.raw.send_realtime_input(**kwargs)
        except Exception as error:raise safe_error(error) from None

    async def send_audio(self,frame):
        async with self.lock:
            if (self.closed or self.flushing or self.error or frame.segment_id!=self.request.context.segment_id or
                frame.sample_rate!=16000 or frame.sequence<=self.sequence or frame.source_offset_ms<self.offset):
                raise failure(ErrorCode.INVALID_AUDIO,'Gemini audio kadra secība vai formāts nav derīgs.')
            if self.samples+len(frame.pcm)//2>16000*600 or time.monotonic()-self.started>=600:
                raise failure(ErrorCode.TIMEOUT,'Sasniegts Gemini tiešraides sesijas 10 minūšu limits.')
            if self.manual and not self.activity:
                await self._send(activity_start=self.types.ActivityStart());self.activity=True
            self.sequence=frame.sequence;self.offset=frame.source_offset_ms;self.samples+=len(frame.pcm)//2
            self.buffer.extend(frame.pcm)
            while len(self.buffer)>=3200:
                chunk=bytes(self.buffer[:3200]);del self.buffer[:3200]
                await self._send(audio=self.types.Blob(data=chunk,mime_type='audio/pcm;rate=16000'))
                self.sent_samples+=len(chunk)//2

    async def flush(self):
        async with self.lock:
            if self.closed:return
            if not self.flushing:
                self.flushing=True;self.flush_final_count=self.final_count
                if self.buffer:
                    chunk=bytes(self.buffer);self.buffer.clear()
                    await self._send(audio=self.types.Blob(data=chunk,mime_type='audio/pcm;rate=16000'))
                    self.sent_samples+=len(chunk)//2
                if not self.samples:
                    self.finished=True;await self.queue.put(None);return
                self.end_sent=True
                self.boundary_type='activity_end' if self.manual else 'audio_stream_end'
                if self.manual:await self._send(activity_end=self.types.ActivityEnd())
                else:await self._send(audio_stream_end=True)
        try:
            async with asyncio.timeout(self.final_timeout):
                while not self.finished:
                    self.changed.clear()
                    if self.error:raise ProviderFailure(self.error)
                    unresolved=any(not s['final'] for s in self.spans.values())
                    # Authoritative finals need no agent turn_complete event.
                    if not unresolved and (self.final_count>self.flush_final_count or self.flush_finished_transcription):
                        self.finished=True;await self.queue.put(None);break
                    await self.changed.wait()
        except TimeoutError:
            if self.final_count and not any(not s['final'] for s in self.spans.values()):
                raise failure(ErrorCode.TIMEOUT,'Gemini apstiprinātais teksts ir saglabāts. Pēc apturēšanas nav saņemts audio beigu apstiprinājums; pēdējā audio daļa nav pārbaudāma.') from None
            raise

    def diagnostics(self):
        return {'audio_samples_accepted':self.samples,'audio_samples_sent':self.sent_samples,'boundary_type':self.boundary_type,'boundary_sent':self.end_sent,
            'server_messages':self.message_count,'final_spans':self.final_count,'final_spans_at_flush':self.flush_final_count,
            'post_end_final_messages':self.post_end_finals,'finished_marker':self.flush_finished_transcription,
            'turn_after_end':self.turn_after_end,'unresolved_spans':sum(not s['final'] for s in self.spans.values()),
            'final_timeout_seconds':self.final_timeout,'error_code':self.error.code.value if self.error else None}

    async def _emit(self,event):
        async with asyncio.timeout(self.timeout):await self.queue.put(event)

    async def _transcript(self,data,final):
        if self.end_sent and final and data.get('finished') is True:self.flush_finished_transcription=True
        text=data.get('text')
        if text is None and data.get('finished') is True:
            if self.end_sent and final:self.flush_finished_transcription=True
            return
        if not isinstance(text,str) or len(text)>1024*1024:raise ValueError('invalid transcript')
        start=data.get('start_offset')
        key=self.offsets.get(start,self.current) if start is not None else self.current
        if key is None:
            if self.index>=10000:raise ValueError('span limit')
            key=f'utterance_{self.index}';self.spans[key]={'index':self.index,'revision':0,'final':False,'text':None}
            self.index+=1
        if start is not None:self.offsets[start]=key
        span=self.spans[key]
        if span['final']:
            if not final or text==span['text']:return  # Identified duplicates only; preserve legitimate repeats.
        elif text==span['text'] and not final:return
        if final and not span['final']:self.final_count+=1
        span.update(revision=span['revision']+1,final=final,text=text)
        self.current=None if final else key
        kind=FinalTranscriptEvent if final else PartialTranscriptEvent
        await self._emit(kind(context=self.request.context,span_id=key,span_index=span['index'],revision=span['revision'],text=text))
        if self.end_sent and final and data.get('finished') is True:self.flush_finished_transcription=True

    async def _handle(self,message):
        self.message_count+=1
        data=message.model_dump(mode='json',exclude_none=True) if hasattr(message,'model_dump') else message
        if data.get('go_away'):raise failure(ErrorCode.DISCONNECTED,'Gemini sesija tiek slēgta; automātiska atkārtota savienošana nenotiek.')
        if data.get('error'):raise failure(ErrorCode.DISCONNECTED,'Gemini tiešraides pakalpojums atgrieza kļūdu.')
        content=data.get('server_content',{})
        if content.get('interrupted'):raise failure(ErrorCode.DISCONNECTED,'Gemini transkripcijas plūsma pārtraukta.')
        if content.get('interim_input_transcription') is not None:await self._transcript(content['interim_input_transcription'],False)
        if content.get('input_transcription') is not None:
            if self.end_sent:self.post_end_finals+=1
            await self._transcript(content['input_transcription'],True)
        if self.end_sent and (content.get('turn_complete') or content.get('interaction_status')=='IDLE'):self.turn_after_end=True
        self.changed.set()

    async def _read(self):
        try:
            while not self.closed and not self.finished:
                received=False
                # SDK receive() ends per turn, not per connection.
                async for message in self.raw.receive():
                    received=True;await self._handle(message)
                    if self.closed or self.finished:return
                if not received:raise failure(ErrorCode.DISCONNECTED,'Gemini audio savienojums pārtrūka.')
        except asyncio.CancelledError:raise
        except Exception as error:
            self.error=error.details if isinstance(error,ProviderFailure) else safe_error(error).details
            self.changed.set()
            await self._emit(ErrorEvent(context=self.request.context,error=self.error));await self.queue.put(None)

    async def events(self):
        while True:
            if self.closed and self.queue.empty():return
            event=await self.queue.get()
            if event is None:return
            yield event

    async def close(self):
        if self.closed:return
        self.closed=True
        if self.reader and not self.reader.done():self.reader.cancel()
        if self.reader:await asyncio.gather(self.reader,return_exceptions=True)
        failed=False
        for operation in (lambda:self.manager.__aexit__(None,None,None),self.client.aio.aclose,lambda:asyncio.to_thread(self.client.close)):
            try:
                async with asyncio.timeout(self.timeout):await operation()
            except Exception:failed=True
        try:self.queue.put_nowait(None)
        except asyncio.QueueFull:pass
        self.changed.set()
        if failed:raise failure(ErrorCode.DISCONNECTED,'Gemini savienojumu nevar noslēgt.')
