"""Offline-first bounded live fan-out. Source PCM is never replaced by provider audio."""
import asyncio
from array import array
import hashlib
import json
import math
import os
import sys
import wave
from uuid import UUID
from importlib.metadata import version

from pydantic import Field, model_validator
from app.contracts import (Record, ReadingLabel, Mode, Provider, Status, ErrorCode, ErrorDetails,
    CaptureSegment, AttemptRecord, ReadingRecord, EventContext, FinalTranscriptEvent, PartialTranscriptEvent, ErrorEvent)
from app.providers.base import AudioFrame, LiveRequest, ProviderFailure
from app.transcripts import _write_atomic, TranscriptStorageError


class LiveStart(Record):
    reading: ReadingLabel
    segment_id: UUID
    sample_rate: int = Field(ge=8000, le=96000, strict=True)
    channels: int = Field(ge=1, le=8, strict=True)
    source_start_sample: int = Field(ge=0, strict=True)


class LiveStop(Record):
    interrupted: bool = Field(default=False, strict=True)
    expected_frames: int | None = Field(default=None, ge=0, strict=True)
    expected_samples: int | None = Field(default=None, ge=0, strict=True)


class FrameHeader(Record):
    sequence: int = Field(ge=0, strict=True)
    segment_id: UUID
    sample_rate: int = Field(ge=8000, le=96000, strict=True)
    channels: int = Field(ge=1, le=8, strict=True)
    frame_samples: int = Field(gt=0, le=9600, strict=True)
    source_start_sample: int = Field(ge=0, strict=True)
    source_end_sample: int = Field(ge=0, strict=True)
    audio_start_sample: int = Field(ge=0, strict=True)
    audio_end_sample: int = Field(ge=0, strict=True)
    missing_input_samples: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode='after')
    def lengths(self):
        if (self.source_end_sample-self.source_start_sample != self.frame_samples or
                self.audio_end_sample-self.audio_start_sample != self.frame_samples or
                self.missing_input_samples > self.frame_samples or
                self.frame_samples > self.sample_rate//10):
            raise ValueError('Audio kadra laiki nav derīgi.')
        return self


class PCMConverter:
    """Stateful mono arithmetic mix and rational weighted-box resampling."""
    def __init__(self, source_rate, target_rate, channels):
        self.source_rate, self.target_rate, self.channels = source_rate, target_rate, channels
        self.remaining = source_rate
        self.energy = 0.0

    def convert(self, payload, *, final=False):
        values = array('h'); values.frombytes(payload)
        if sys.byteorder != 'little': values.byteswap()
        output = array('h')
        for index in range(0, len(values), self.channels):
            value = sum(values[index:index+self.channels])/self.channels
            ticks = self.target_rate
            while ticks:
                amount = min(ticks, self.remaining)
                self.energy += value*amount; self.remaining -= amount; ticks -= amount
                if not self.remaining:
                    output.append(max(-32768,min(32767,round(self.energy/self.source_rate))))
                    self.remaining = self.source_rate; self.energy = 0.0
        if final and self.remaining != self.source_rate:
            output.append(max(-32768,min(32767,round(self.energy/(self.source_rate-self.remaining)))))
            self.remaining = self.source_rate; self.energy = 0.0
        if sys.byteorder != 'little': output.byteswap()
        return output.tobytes()


class FakeLiveSession:
    def __init__(self, request):
        self.request=request; self.received=0; self.queue=asyncio.Queue()
    async def send_audio(self, frame): self.received+=1
    async def flush(self):
        await self.queue.put(FinalTranscriptEvent(context=self.request.context,span_id='demo',span_index=0,
            revision=1,text='DEMO: saņemti audio kadri; tas nav īsts transkripts.'))
        await self.queue.put(None)
    async def close(self): pass
    async def events(self):
        while True:
            event=await self.queue.get()
            if event is None:return
            yield event

class FakeLiveAdapter:
    async def open_live(self, request): return FakeLiveSession(request)


class LiveDistribution:
    def __init__(self, registry, transcripts, *, factory=None, queue_size=128, timeout=5, execution_mode='demo'):
        if execution_mode not in ('demo','paid'):raise ValueError('Invalid live execution mode')
        if execution_mode=='paid' and factory is None:raise ValueError('Paid live routing requires an explicit factory')
        self.execution_mode=execution_mode
        self.final_timeout=90 if execution_mode=='paid' else timeout
        self.registry, self.store = registry, transcripts
        self.factory = factory or (lambda _model: FakeLiveAdapter())
        self.queue_size, self.timeout = queue_size, timeout
        self.run=None; self.start_info=None; self.workers={}; self.source_queue=asyncio.Queue(queue_size)
        self.accepted=0; self.sequence=0; self.source_end=0; self.stopped=False; self.error=None
        self.archived_frames=0;self.archive_status='pending'
        self.persist_lock=asyncio.Lock()
        self.phase='preparing';self.segment_index=0;self.origin=0
        self.segment_info=None;self.lifecycle_lock=asyncio.Lock()
        self.stop_task=None

    async def start(self, info):
        if self.run is not None:raise ValueError('Izpilde jau sākta.')
        self.start_info=info; self.source_end=info.source_start_sample
        self.segment_info=info;self.origin=info.source_start_sample
        self.run=await asyncio.to_thread(self.registry.create_run,Mode.LIVE,demo=self.execution_mode=='demo')
        segment=CaptureSegment(id=info.segment_id,index=0,source_start_ms=0,audio_start_ms=0)
        attempts=tuple(AttemptRecord(model_entry_id=m.entry_id,segment_ids=(segment.id,),status=Status.RUNNING) for m in self.run.models)
        self.run=self.run.evolve(status=Status.RUNNING,readings=(ReadingRecord(label=info.reading,
            status=Status.RUNNING,segments=(segment,),attempts=attempts),))
        await asyncio.to_thread(self.store.checkpoint_run,self.run)
        folder='Try1' if info.reading==ReadingLabel.A else 'Try2'
        self.folder=self.store._path(self.run.id,folder); self.folder.mkdir(parents=True,exist_ok=True)
        await self._save_distribution()
        self.archive_task=asyncio.create_task(self._archive())
        await self._open_workers(attempts)
        self.phase='ready'
        return self.snapshot()

    async def _open_workers(self, attempts):
        info=self.segment_info
        previous=self.workers
        self.workers={}
        for model,attempt in zip(self.run.models,attempts):
            context=EventContext(run_id=self.run.id,reading=info.reading,attempt_id=attempt.id,
                model_entry_id=model.entry_id,segment_id=info.segment_id,segment_index=self.segment_index)
            state=dict(model=model,attempt=attempt,context=context,queue=asyncio.Queue(self.queue_size),
                ready=asyncio.Event(),sending_done=asyncio.Event(),status='preparing',error=None,sent_sequences=[],output_samples=0,
                target_rate=16000 if model.provider in (Provider.GEMINI,Provider.MISTRAL) else 24000, executions=[])
            old=previous.get(model.entry_id)
            if old:
                state['error']=old['error']
                state['sent_sequences']=list(old['sent_sequences'])
                state['output_samples']=old['output_samples']
                state['executions']=list(old['executions'])
            self.workers[model.entry_id]=state
            state['task']=asyncio.create_task(self._worker(state))
        await asyncio.gather(*(s['ready'].wait() for s in self.workers.values()))

    def offer(self, header, pcm):
        if self.stopped: raise ValueError('Audio izpilde jau noslēgta.')
        # Excluded audio cannot advance clocks, fill queues or be replayed later.
        if self.phase=='paused':return False
        if self.phase not in ('ready','recording'):raise ValueError('Audio savienojumi vēl nav gatavi.')
        if self.error:raise ValueError(self.error.message)
        info=self.segment_info
        if self.sequence>=100000 or (self.accepted+header.frame_samples)/info.sample_rate>1800:
            self.error=ErrorDetails(code=ErrorCode.QUEUE_OVERFLOW,message='Sasniegts audio izpildes ilguma vai kadru limits.')
            raise ValueError(self.error.message)
        if (header.segment_id!=info.segment_id or header.sample_rate!=info.sample_rate or header.channels!=info.channels or
                header.sequence!=self.sequence or header.source_start_sample!=self.source_end or
                header.audio_start_sample!=self.accepted or len(pcm)!=header.frame_samples*header.channels*2):
            self.error=ErrorDetails(code=ErrorCode.INVALID_AUDIO,message='Audio secībā ir atstarpe vai mainījās formāts.')
            raise ValueError(self.error.message)
        try:self.source_queue.put_nowait((header,pcm))
        except asyncio.QueueFull:
            self.error=ErrorDetails(code=ErrorCode.QUEUE_OVERFLOW,message='Avota saglabāšanas rinda pārpildīta; ieraksts ir nepilnīgs.')
            raise ValueError(self.error.message) from None
        self.sequence+=1;self.accepted+=header.frame_samples;self.source_end=header.source_end_sample
        self.phase='recording'
        for state in self.workers.values():
            if state['error'] or state['status']!='running':continue
            try:state['queue'].put_nowait((header,pcm))
            except asyncio.QueueFull:
                state['error']=ErrorDetails(code=ErrorCode.QUEUE_OVERFLOW,message='Modeļa audio rinda pārpildīta; rezultāts ir nepilnīgs.')
                state['task'].cancel()
        return True

    async def _archive(self):
        info=self.start_info
        try:
            await asyncio.to_thread(self._open_archive,info)
            while True:
                item=await self.source_queue.get()
                try:
                    if item is None:break
                    await asyncio.to_thread(self._write_source,*item)
                finally:self.source_queue.task_done()
        except Exception:
            self.archive_status='failed'
            self.error=ErrorDetails(code=ErrorCode.STORAGE,message='Avota audio saglabāšana neizdevās.')
        finally:
            try:
                await asyncio.to_thread(self._close_archive)
                if self.archive_status!='failed':self.archive_status='saved'
            except Exception:
                self.archive_status='failed'
                self.error=ErrorDetails(code=ErrorCode.STORAGE,message='Avota audio failu nevar noslēgt.')

    def _open_archive(self, info):
        self.audio_file=(self.folder/'audio.partial.wav').open('xb')
        self.wav=wave.open(self.audio_file,'wb')
        self.wav.setparams((info.channels,2,info.sample_rate,0,'NONE','not compressed'))
        self.journal=(self.folder/'source_frames.jsonl').open('x',encoding='utf-8')
    def _write_source(self, header, pcm):
        self.wav.writeframes(pcm)
        self.journal.write(json.dumps({**header.model_dump(mode='json'),'sha256':hashlib.sha256(pcm).hexdigest()})+'\n')
        self.journal.flush()
        self.archived_frames+=1
    def _close_archive(self):
        if hasattr(self,'wav'):self.wav.close()
        for stream in (getattr(self,'audio_file',None),getattr(self,'journal',None)):
            if stream:
                try:stream.flush();os.fsync(stream.fileno())
                finally:stream.close()
        path=self.folder/'audio.partial.wav'
        if path.exists():path.rename(self.folder/'audio.wav')

    def _flush_archive(self):
        for stream in (self.audio_file,self.journal):
            stream.flush();os.fsync(stream.fileno())

    async def _receiver(self, state, session):
        async for event in session.events():
            if event.context!=state['context']:raise ValueError('context')
            try:
                await asyncio.to_thread(self.store.accept,event)
            except TranscriptStorageError:
                raise ProviderFailure(ErrorDetails(code=ErrorCode.STORAGE,
                    message='Modeļa transkripta kontrolpunktu nevar saglabāt diskā.')) from None
            if isinstance(event,ErrorEvent) and event.fatal:raise ProviderFailure(event.error)

    async def _sender(self, state, session, converter):
        last=None
        while True:
            item=await state['queue'].get()
            if item is None:break
            header,payload=item;last=header
            pcm=await asyncio.to_thread(converter.convert,payload)
            if pcm:
                async with asyncio.timeout(self.timeout):
                    await session.send_audio(AudioFrame(segment_id=header.segment_id,sequence=header.sequence,
                        source_offset_ms=round((header.source_start_sample-self.origin)*1000/header.sample_rate),
                        sample_rate=state['target_rate'],pcm=pcm))
                state['output_samples']+=len(pcm)//2
            state['sent_sequences'].append(header.sequence)
        tail=converter.convert(b'',final=True)
        if tail and last:
            async with asyncio.timeout(self.timeout):
                await session.send_audio(AudioFrame(segment_id=last.segment_id,sequence=last.sequence+1,
                    source_offset_ms=round((last.source_end_sample-self.origin)*1000/last.sample_rate),
                    sample_rate=state['target_rate'],pcm=tail))
            state['output_samples']+=len(tail)//2

    async def _worker(self, state):
        session=None;sender=None;receiver=None
        failure_stage='open'
        try:
            if state['error']:return  # Failed models remain incomplete; no implicit reconnect.
            async with asyncio.timeout(self.timeout):
                session=await self.factory(state['model']).open_live(LiveRequest(context=state['context'],model=state['model']))
            execution={'segment_id':str(state['context'].segment_id),'config':getattr(session,'config',None),
                'commit_policy':('DEMO synthetic final on flush' if self.execution_mode=='demo'
                    else 'explicit 10 s commits; final tail padded to 100 ms' if state['model'].provider==Provider.OPENAI
                    else 'activity boundary; 10 minute session limit' if state['model'].provider==Provider.GEMINI
                    else 'confirmed append-only deltas; 100 ms packets + unchanged tail; end on pause/stop; done required for complete' if state['model'].provider==Provider.MISTRAL
                    else 'explicit serialized 20 s commits; unchanged tail'),
                'padding_samples':0,'sdk_version':version({'openai':'openai','gemini':'google-genai','elevenlabs':'elevenlabs','mistral':'mistralai'}[state['model'].provider.value])
                    if self.execution_mode=='paid' else None}
            state['executions'].append(execution)
            await self._save_distribution()
            converter=PCMConverter(self.segment_info.sample_rate,state['target_rate'],self.segment_info.channels)
            state['status']='running';state['ready'].set()
            receiver=asyncio.create_task(self._receiver(state,session))
            sender=asyncio.create_task(self._sender(state,session,converter))
            failure_stage='stream'
            done,_=await asyncio.wait((sender,receiver),return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception():
                    failure_stage='receive_checkpoint' if task is receiver else 'send_audio'
                    raise task.exception()
            if sender not in done:raise ValueError('receiver ended early')
        except asyncio.CancelledError:
            if state['error'] is None:state['error']=ErrorDetails(code=ErrorCode.DISCONNECTED,message='Audio izpilde pārtraukta.')
        except Exception as error:
            if session and state['executions']:
                state['executions'][-1]['local_failure']={'stage':failure_stage,'exception_type':type(error).__name__}
            state['error']=error.details if isinstance(error,ProviderFailure) else ErrorDetails(
                code=ErrorCode.TIMEOUT if isinstance(error,TimeoutError) else ErrorCode.DISCONNECTED,
                message='Modeļa audio izpildes laiks pārsniegts.' if isinstance(error,TimeoutError) else 'Modeļa audio savienojums pārtrūka.')
        finally:
            state['ready'].set()
            if sender and not sender.done():sender.cancel()
            if sender:await asyncio.gather(sender,return_exceptions=True)
            state['sending_done'].set()
            if session:
                try:
                    async with asyncio.timeout(self.final_timeout):await session.flush()
                    if receiver:
                        async with asyncio.timeout(self.timeout):await asyncio.shield(receiver)
                except Exception as error:
                    if state['error'] is None:state['error']=error.details if isinstance(error,ProviderFailure) else ErrorDetails(code=ErrorCode.TIMEOUT,message='Modeļa gala teksts nav apstiprināts.')
                finally:
                    if receiver and not receiver.done():receiver.cancel()
                    if receiver:await asyncio.gather(receiver,return_exceptions=True)
                    try:
                        async with asyncio.timeout(self.timeout):await session.close()
                    except Exception:
                        if state['error'] is None:state['error']=ErrorDetails(code=ErrorCode.DISCONNECTED,message='Modeļa savienojumu nevar noslēgt.')
                    if state['executions']:
                        state['executions'][-1]['padding_samples']=getattr(session,'padding_samples',0)
                        if hasattr(session,'diagnostics'):state['executions'][-1]['protocol']=session.diagnostics()
            state['status']='incomplete' if state['error'] else 'complete'
            try:
                checkpoint=await asyncio.to_thread(self.store.load,self.run.id)
                unresolved=any(isinstance(span,PartialTranscriptEvent) and span.context==state['context']
                    for attempt in checkpoint.attempts if attempt.attempt_id==state['attempt'].id for span in attempt.spans)
                if unresolved and not state['error']:
                    state['error']=ErrorDetails(code=ErrorCode.DISCONNECTED,message='Daļa teksta palika bez gala apstiprinājuma.')
                    state['status']='incomplete'
            except Exception:
                if not state['error']:state['error']=ErrorDetails(code=ErrorCode.STORAGE,message='Teksta kontrolpunktu nevar pārbaudīt.')
                state['status']='incomplete'

    async def _drain_workers(self):
        async def end_worker(state):
            if not state['task'].done():
                try:
                    async with asyncio.timeout(self.timeout):await state['queue'].put(None)
                except TimeoutError:
                    state['error']=ErrorDetails(code=ErrorCode.QUEUE_OVERFLOW,message='Modeļa rindas noslēgšana neizdevās.')
                    state['task'].cancel()
                # Bound queued sends separately. Never cancel an already running
                # finalization merely because its network-send deadline expired.
                try:
                    async with asyncio.timeout(self.timeout):await state['sending_done'].wait()
                except TimeoutError:
                    if not state['error']:state['error']=ErrorDetails(code=ErrorCode.TIMEOUT,message='Modeļa audio rindas iztukšošanas laiks pārsniegts.')
                    state['task'].cancel()
        await asyncio.gather(*(end_worker(state) for state in self.workers.values()))
        await asyncio.gather(*(s['task'] for s in self.workers.values()),return_exceptions=True)

    async def _close_segment(self):
        current=(await asyncio.to_thread(self.store.load,self.run.id)).run
        reading=current.readings[0]
        segment=reading.segments[-1]
        if segment.source_end_ms is None:
            segment=segment.evolve(source_end_ms=round((self.source_end-self.origin)*1000/self.start_info.sample_rate),
                audio_end_ms=round(self.accepted*1000/self.start_info.sample_rate))
            reading=reading.evolve(segments=(*reading.segments[:-1],segment))
            await asyncio.to_thread(self.store.checkpoint_run,current.evolve(readings=(reading,)))

    async def pause(self):
        async with self.lifecycle_lock:
            if self.phase not in ('ready','recording'):raise ValueError('Izpildi nevar pauzēt.')
            self.phase='pausing'
            await self._drain_workers()
            try:
                async with asyncio.timeout(self.timeout):await self.source_queue.join()
                if self.archive_status=='failed':raise ValueError('archive')
                await asyncio.to_thread(self._flush_archive)
            except Exception:
                self.error=ErrorDetails(code=ErrorCode.STORAGE,message='Pirms pauzes avota audio nevar saglabāt.')
            await self._close_segment()
            self.phase='paused'
            await self._save_distribution()
            return self.snapshot()

    async def resume(self, info):
        async with self.lifecycle_lock:
            if self.phase!='paused' or self.error:raise ValueError('Izpildi nevar turpināt.')
            first=self.start_info
            current=(await asyncio.to_thread(self.store.load,self.run.id)).run
            reading=current.readings[0]
            if (info.reading!=first.reading or info.sample_rate!=first.sample_rate or info.channels!=first.channels or
                    info.source_start_sample<self.source_end or any(s.id==info.segment_id for s in reading.segments)):
                raise ValueError('Jaunā segmenta identitāte, laiks vai formāts nav derīgs.')
            self.phase='preparing'
            self.segment_index+=1;self.segment_info=info;self.source_end=info.source_start_sample
            segment=CaptureSegment(id=info.segment_id,index=self.segment_index,
                source_start_ms=round((self.source_end-self.origin)*1000/first.sample_rate),
                audio_start_ms=round(self.accepted*1000/first.sample_rate))
            attempts=tuple(a.evolve(segment_ids=(*a.segment_ids,info.segment_id)) for a in reading.attempts)
            await asyncio.to_thread(self.store.checkpoint_run,current.evolve(readings=(reading.evolve(
                segments=(*reading.segments,segment),attempts=attempts),)))
            await self._open_workers(attempts)
            self.phase='ready'
            await self._save_distribution()
            return self.snapshot()

    async def _save_distribution(self):
        async with self.persist_lock:
            await asyncio.to_thread(_write_atomic,self.folder/'distribution.json',json.dumps(self.snapshot(),ensure_ascii=False).encode())

    async def stop(self, interrupted=False):
        # The socket handler can be cancelled by page closure; it must not cancel saving.
        if interrupted and not self.stopped and not self.error:
            self.error=ErrorDetails(code=ErrorCode.DISCONNECTED,message='Audio uztveršana pārtraukta.')
        if self.stop_task is None or self.stop_task.cancelled() or (self.stop_task.done() and self.stop_task.exception() is not None):
            self.stop_task=asyncio.create_task(self._serialized_stop(interrupted))
        return await asyncio.shield(self.stop_task)

    async def _serialized_stop(self, interrupted):
        async with self.lifecycle_lock:
            return await self._stop(interrupted)

    async def _stop(self, interrupted=False):
        if self.stopped:return self.snapshot()
        self.phase='stopping'
        if interrupted and not self.error:self.error=ErrorDetails(code=ErrorCode.DISCONNECTED,message='Audio uztveršana pārtraukta.')
        # A page disconnect still drains valid queued audio before sealing output.
        await self._drain_workers()
        if not self.archive_task.done():
            try:
                async with asyncio.timeout(self.timeout):await self.source_queue.put(None)
            except TimeoutError:self.archive_task.cancel()
        await asyncio.gather(self.archive_task,*(s['task'] for s in self.workers.values()),return_exceptions=True)
        if self.archived_frames!=self.sequence:
            self.archive_status='failed'
            self.error=ErrorDetails(code=ErrorCode.STORAGE,message='Daļa pieņemtā avota audio nav saglabāta.')
        await self._close_segment()
        for state in self.workers.values():
            if self.error and state['error'] is None:state['error']=self.error
            try:
                attempt=await asyncio.to_thread(self.store.finalize,self.run.id,state['attempt'].id,
                    status=Status.INCOMPLETE if state['error'] else Status.COMPLETE,
                    errors=(state['error'],) if state['error'] else ())
                state['status']='storage_failed' if attempt.save_status!='saved' else ('incomplete' if state['error'] else 'complete')
            except Exception:state['status']='storage_failed'
        current=(await asyncio.to_thread(self.store.load,self.run.id)).run
        status=Status.COMPLETE if not self.error and all(s['status']=='complete' for s in self.workers.values()) else Status.INCOMPLETE
        reading=current.readings[0].evolve(status=status)
        await asyncio.to_thread(self.store.checkpoint_run,current.evolve(status=status,readings=(reading,)))
        self.phase='stopped';self.stopped=True
        await self._save_distribution()
        return self.snapshot()

    def snapshot(self):
        return {'run_id':str(self.run.id),'execution_mode':self.execution_mode,'stopped':self.stopped,
            'state':self.phase,'segment_index':self.segment_index,'segment':self.segment_info.model_dump(mode='json'),
            'source':self.start_info.model_dump(mode='json'),'accepted_samples':self.accepted,'accepted_frames':self.sequence,
            'source_end_sample':self.source_end,'error':self.error.model_dump(mode='json') if self.error else None,
            'archive_status':self.archive_status,'archived_frames':self.archived_frames,
            'models':{str(identity):{'status':s['status'],'error':s['error'].model_dump(mode='json') if s['error'] else None,
                'sent_sequences':s['sent_sequences'],'output_samples':s['output_samples'],'target_rate':s['target_rate'],
                'executions':s['executions'],
                'conversion':'channel arithmetic mean; rational weighted-box; final partial bin average'} for identity,s in self.workers.items()}}
