"""GA transcription-only WebSocket adapters. Lazy SDK; no implicit paid execution."""
import asyncio
import base64
import math
import os

from app.contracts import (Provider, Mode, ErrorCode, ErrorDetails, ErrorEvent,
    PartialTranscriptEvent, FinalTranscriptEvent)
from app.providers.base import ProviderFailure
from app.providers.settings import effective_settings

MODELS={'gpt-transcribe','gpt-live-transcribe','gpt-realtime-whisper'}


def failure(code,message):return ProviderFailure(ErrorDetails(code=code,message=message))


def safe_error(error):
    code=getattr(error,'status_code',None)
    if isinstance(error,dict):code=error.get('code')
    category=(ErrorCode.AUTHENTICATION if code in (401,403,'invalid_api_key','authentication_error') else
        ErrorCode.RATE_LIMIT if code in (429,'rate_limit_exceeded') else
        ErrorCode.UNAVAILABLE_MODEL if code in (404,'model_not_found') else
        ErrorCode.UNSUPPORTED_SETTINGS if code in (400,'invalid_request_error','invalid_value') else
        ErrorCode.TIMEOUT if isinstance(error,TimeoutError) else ErrorCode.DISCONNECTED)
    return ErrorDetails(code=category,message={
        ErrorCode.AUTHENTICATION:'OpenAI autentifikācija neizdevās.',
        ErrorCode.RATE_LIMIT:'Sasniegts OpenAI pieprasījumu limits.',
        ErrorCode.UNAVAILABLE_MODEL:'OpenAI modelis nav pieejams.',
        ErrorCode.UNSUPPORTED_SETTINGS:'OpenAI noraidīja transkripcijas iestatījumus vai audio.',
        ErrorCode.TIMEOUT:'OpenAI transkripcijas gaidīšanas laiks pārsniegts.',
        ErrorCode.DISCONNECTED:'OpenAI audio savienojums pārtrūka.'}[category])


def session_config(request):
    model=request.model
    if (model.provider!=Provider.OPENAI or model.mode!=Mode.LIVE or model.model_id not in MODELS or
        model.capability_profile_id!=f'openai:{model.model_id}:live'):
        raise failure(ErrorCode.UNAVAILABLE_MODEL,'Šis adapteris neatbalsta izvēlēto OpenAI modeli.')
    try:resolved=effective_settings(Provider.OPENAI,model.requested_settings)
    except ValueError:raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'OpenAI valodas iestatījums nav derīgs.') from None
    settings=model.effective_settings
    whisper=model.model_id=='gpt-realtime-whisper'
    if (model.capability_profile_version!=1 or settings!=resolved or settings.formatting!='default' or
        settings.turn_detection not in ('default','manual') or
        (settings.latency is not None and not whisper) or (whisper and (settings.prompt or settings.keywords)) or
        any(any(c in word for c in '<>\r\n') for word in settings.keywords)):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'Izvēlētie OpenAI tiešraides iestatījumi nav atbalstīti.')
    transcription={'model':model.model_id}
    if settings.language_hint:
        transcription.update({'language':settings.language_hint} if whisper else {'languages':[settings.language_hint]})
    if settings.prompt:transcription['prompt']=settings.prompt
    if settings.keywords:transcription['keywords']=list(settings.keywords)
    if settings.latency:transcription['delay']=settings.latency.value
    return {'type':'transcription','audio':{'input':{'format':{'type':'audio/pcm','rate':24000},
        'transcription':transcription,'turn_detection':None,'noise_reduction':None}}}


class OpenAILiveAdapter:
    def __init__(self, *, timeout_seconds=10, commit_seconds=10, final_timeout_seconds=None, client_factory=None, allow_paid=False):
        if not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=120:raise ValueError('Invalid timeout')
        if not math.isfinite(commit_seconds) or not .1<=commit_seconds<=30:raise ValueError('Invalid commit interval')
        self.timeout=timeout_seconds;self.commit_samples=round(commit_seconds*24000)
        self.final_timeout=timeout_seconds if final_timeout_seconds is None else final_timeout_seconds
        if not math.isfinite(self.final_timeout) or not 0<self.final_timeout<=120:raise ValueError('Invalid final timeout')
        self.factory=client_factory;self.allow_paid=allow_paid

    async def open_live(self, request):
        config=session_config(request)
        if not self.allow_paid:
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'Maksas tiešraides izpilde nav iespējota.')
        key=os.environ.get('OPENAI_API_KEY','').strip()
        if not key:raise failure(ErrorCode.AUTHENTICATION,'Nav konfigurēta OPENAI_API_KEY atslēga.')
        try:import openai
        except ImportError:raise failure(ErrorCode.INTERNAL,'OpenAI Python pakotne nav pieejama.') from None
        client=None;session=None
        try:
            client=(self.factory or openai.AsyncOpenAI)(api_key=key,base_url='https://api.openai.com/v1',
                max_retries=0,timeout=self.timeout)
            manager=client.realtime.connect(extra_query={'intent':'transcription'},max_retries=0,
                websocket_connection_options={'open_timeout':self.timeout},max_queue_size=1024*1024)
            async with asyncio.timeout(self.timeout):connection=await manager.__aenter__()
            session=OpenAILiveSession(request,config,client,connection,self.timeout,self.commit_samples)
            session.final_timeout=self.final_timeout
            async with asyncio.timeout(self.timeout):await session.initialize()
            return session
        except BaseException as error:
            if session:
                try:await session.close()
                except Exception:pass
            elif client:
                try:
                    async with asyncio.timeout(self.timeout):await client.close()
                except Exception:pass
            if isinstance(error,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):raise
            raise error if isinstance(error,ProviderFailure) else ProviderFailure(safe_error(error)) from None


class OpenAILiveSession:
    def __init__(self,request,config,client,connection,timeout,commit_samples):
        self.request=request;self.config=config;self.client=client;self.connection=connection
        self.timeout=timeout;self.commit_samples=commit_samples
        self.final_timeout=timeout;self.event_counts={}
        self.queue=asyncio.Queue(256);self.lock=asyncio.Lock();self.changed=asyncio.Event()
        self.seen=set();self.links={};self.order_links={};self.indices={};self.texts={};self.revisions={};self.finals=set();self.waiting=[]
        self.commits=0;self.buffer_samples=0;self.padding_samples=0;self.last_sequence=-1;self.last_offset=-1
        self.flushing=False;self.finished=False;self.closed=False;self.error=None;self.reader=None

    async def initialize(self):
        await self.connection.send({'type':'session.update','session':self.config})
        while True:
            event=await self.connection.recv();event=event.model_dump(mode='json') if hasattr(event,'model_dump') else event
            if event['type']=='error':raise ProviderFailure(safe_error(event.get('error',{})))
            if event['type']=='session.updated':
                session=event.get('session',{})
                actual=session.get('audio',{}).get('input',{})
                if (session.get('type')!='transcription' or actual.get('format')!={'type':'audio/pcm','rate':24000} or
                    actual.get('turn_detection') is not None or actual.get('noise_reduction') is not None or
                    any(actual.get('transcription',{}).get(k)!=v for k,v in self.config['audio']['input']['transcription'].items())):
                    raise failure(ErrorCode.UNSUPPORTED_SETTINGS,'OpenAI sesija neapstiprināja pieprasītos iestatījumus.')
                break
        self.reader=asyncio.create_task(self._read())

    async def _send(self,event):
        try:
            async with asyncio.timeout(self.timeout):await self.connection.send(event)
        except Exception as error:raise ProviderFailure(safe_error(error)) from None

    async def send_audio(self,frame):
        async with self.lock:
            if (self.closed or self.flushing or self.error or frame.segment_id!=self.request.context.segment_id or
                frame.sample_rate!=24000 or frame.sequence<=self.last_sequence or frame.source_offset_ms<self.last_offset):
                raise failure(ErrorCode.INVALID_AUDIO,'OpenAI audio kadra secība vai formāts nav derīgs.')
            await self._send({'type':'input_audio_buffer.append','audio':base64.b64encode(frame.pcm).decode('ascii')})
            self.last_sequence=frame.sequence;self.last_offset=frame.source_offset_ms
            self.buffer_samples+=len(frame.pcm)//2
            if self.buffer_samples>=self.commit_samples:await self._commit()

    async def _commit(self):
        self.commits+=1;self.buffer_samples=0
        await self._send({'type':'input_audio_buffer.commit'})

    async def flush(self):
        async with self.lock:
            if self.closed:return
            if not self.flushing:
                self.flushing=True
                if self.buffer_samples:
                    if self.buffer_samples<2400:
                        self.padding_samples=2400-self.buffer_samples
                        await self._send({'type':'input_audio_buffer.append','audio':base64.b64encode(b'\0\0'*self.padding_samples).decode('ascii')})
                    await self._commit()
        # Wait for commit acknowledgements AND final transcripts; never guess from text arrival.
        async with asyncio.timeout(self.final_timeout):
            while not self.finished:
                self.changed.clear()
                if self.error:raise ProviderFailure(self.error)
                if self._done():
                    await self.queue.put(None);self.finished=True;break
                await self.changed.wait()

    def _done(self):return len(self.links)==self.commits and set(self.links)<=self.indices.keys() and set(self.links)<=self.finals and not self.waiting

    def diagnostics(self):
        return {'commits_sent':self.commits,'commit_acknowledgements':len(self.links),
            'ordered_items':len(self.indices),'final_items':len(self.finals),'pending_order_events':len(self.waiting),
            'partial_items':len(set(self.texts)-self.finals),'final_timeout_seconds':self.final_timeout,
            'event_counts':dict(self.event_counts),'error_code':self.error.code.value if self.error else None}

    async def _emit(self,event):
        async with asyncio.timeout(self.timeout):await self.queue.put(event)

    async def _transcript(self,event):
        identity=event['item_id']
        if identity not in self.indices:
            if len(self.waiting)>=256:raise ValueError('pending items')
            self.waiting.append(event);return
        if event.get('content_index',0)!=0:raise ValueError('content index')
        if identity in self.finals:return
        final=event['type'].endswith('.completed')
        text=event['transcript'] if final else self.texts.get(identity,'')+event['delta']
        if not isinstance(text,str) or len(text)>1024*1024:raise ValueError('text')
        self.texts[identity]=text;self.revisions[identity]=self.revisions.get(identity,0)+1
        if final:self.finals.add(identity)
        kind=FinalTranscriptEvent if final else PartialTranscriptEvent
        await self._emit(kind(context=self.request.context,span_id=identity,span_index=self.indices[identity],
            revision=self.revisions[identity],text=text))

    async def _handle(self,event):
        event_id=event.get('event_id')
        if event_id:
            if event_id in self.seen:return
            if len(self.seen)>=100000:raise ValueError('event limit')
            self.seen.add(event_id)
        kind=event['type']
        counted=kind if kind in ('input_audio_buffer.committed','conversation.item.created',
            'conversation.item.input_audio_transcription.delta','conversation.item.input_audio_transcription.completed',
            'conversation.item.input_audio_transcription.failed','error') else 'other'
        self.event_counts[counted]=self.event_counts.get(counted,0)+1
        if kind in ('error','conversation.item.input_audio_transcription.failed'):
            raise ProviderFailure(safe_error(event.get('error',{})))
        if kind in ('input_audio_buffer.committed','conversation.item.created'):
            if kind=='conversation.item.created':
                item=event.get('item',{})
                if item.get('role')!='user' or not any(c.get('type')=='input_audio' for c in item.get('content',[])):return
                identity=item['id']
            else:
                identity=event['item_id']
                self.links[identity]=event.get('previous_item_id')
                if len(self.links)>self.commits:raise ValueError('unexpected commit')
            previous=event.get('previous_item_id')
            if identity in self.order_links and self.order_links[identity]!=previous:raise ValueError('item changed')
            self.order_links[identity]=previous
            if len(self.order_links)>10000:raise ValueError('item limit')
            for _ in range(len(self.order_links)):
                for item,parent in self.order_links.items():
                    if item not in self.indices and (parent is None or parent in self.indices):
                        index=0 if parent is None else self.indices[parent]+1
                        if index in self.indices.values():raise ValueError('item fork')
                        self.indices[item]=index
            waiting=self.waiting;self.waiting=[]
            for pending in waiting:await self._transcript(pending)
        elif kind in ('conversation.item.input_audio_transcription.delta','conversation.item.input_audio_transcription.completed'):
            await self._transcript(event)
        self.changed.set()

    async def _read(self):
        try:
            async for raw in self.connection:
                event=raw.model_dump(mode='json') if hasattr(raw,'model_dump') else raw
                await self._handle(event)
            if not self.closed and not self.finished:raise ValueError('early EOF')
        except asyncio.CancelledError:raise
        except Exception as error:
            self.error=error.details if isinstance(error,ProviderFailure) else safe_error(error)
            self.changed.set()
            await self._emit(ErrorEvent(context=self.request.context,error=self.error))
            await self.queue.put(None)

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
        cleanup_failed=False
        for resource in (self.connection,self.client):
            try:
                async with asyncio.timeout(self.timeout):await resource.close()
            except Exception:
                cleanup_failed=True
        try:self.queue.put_nowait(None)
        except asyncio.QueueFull:pass
        self.changed.set()
        if cleanup_failed:raise failure(ErrorCode.DISCONNECTED,'OpenAI savienojumu nevar noslēgt.')
