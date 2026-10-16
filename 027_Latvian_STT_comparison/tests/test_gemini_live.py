import asyncio
import base64
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from google import genai
from google.genai import errors
from app.contracts import EventContext, ModelEntry, ModelSnapshot, TranscriptionSettings, FinalTranscriptEvent
from app.providers.base import AudioFrame, LiveRequest, ProviderFailure
from app.providers.settings import effective_settings
from app.providers.gemini_live import GeminiLiveAdapter


class Socket:
    def __init__(self):self.queue=asyncio.Queue();self.sent=[];self.closed=0
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed+=1
    async def recv(self,*args,**kwargs):
        item=await self.queue.get()
        if isinstance(item,Exception):raise item
        return json.dumps(item).encode()
    def put(self,item):self.queue.put_nowait(item)
    async def send(self,payload):
        data=json.loads(payload);self.sent.append(data)
        if 'setup' in data:self.put({'setupComplete':{'sessionId':'test'}})
        realtime=data.get('realtime_input',{})
        if realtime.get('audioStreamEnd') or 'activityEnd' in realtime:
            self.finish()
    def finish(self):
        self.put({'serverContent':{'interimInputTranscription':{'text':'Rīg'}}})
        self.put({'serverContent':{'interimInputTranscription':{'text':'Rīga, ābols!'}}})
        self.put({'serverContent':{'inputTranscription':{'text':'Rīga, ābols!','startOffset':'0s','finished':True}}})
        self.put({'serverContent':{'turnComplete':True}})


class GeminiLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedicated_final_without_agent_turn_complete_finishes(self):
        class Dedicated(Socket):
            def finish(inner):
                inner.put({'serverContent':{'inputTranscription':{'text':'Authoritative final'}}})
        self.socket=Dedicated();request=self.request()
        session=await self.adapter(timeout_seconds=.3).open_live(request)
        collector=asyncio.create_task(self.collect(session))
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'*1600))
        await session.flush();events=await collector
        self.assertEqual(len(events),1);self.assertIsInstance(events[0],FinalTranscriptEvent)
        self.assertFalse(session.diagnostics()['turn_after_end'])
        self.assertEqual(session.diagnostics()['post_end_final_messages'],1)
        self.assertEqual(session.diagnostics()['audio_samples_sent'],1600)
        await session.close()

    async def test_preexisting_final_and_silent_unacknowledged_tail_stays_incomplete(self):
        class SilentEnd(Socket):
            def finish(inner):pass
        self.socket=SilentEnd();request=self.request()
        session=await self.adapter(timeout_seconds=.2).open_live(request)
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'*1600))
        self.socket.put({'serverContent':{'inputTranscription':{'text':'Earlier final','startOffset':'0s'}}})
        await asyncio.wait_for(session.queue.get(),1)
        with self.assertRaises(ProviderFailure) as failure:await session.flush()
        self.assertEqual(failure.exception.details.code,'timeout')
        self.assertIn('audio beigu',failure.exception.details.message)
        diagnostic=session.diagnostics()
        self.assertEqual(diagnostic['final_spans'],1);self.assertEqual(diagnostic['post_end_final_messages'],0)
        self.assertNotIn('Earlier final',str(diagnostic))
        await session.close()

    async def test_finished_marker_without_agent_turn_does_not_duplicate_existing_text(self):
        class Marker(Socket):
            def finish(inner):inner.put({'serverContent':{'inputTranscription':{'finished':True}}})
        self.socket=Marker();request=self.request()
        session=await self.adapter(timeout_seconds=.3).open_live(request)
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'*1600))
        self.socket.put({'serverContent':{'inputTranscription':{'text':'Existing final'}}})
        await asyncio.wait_for(session.queue.get(),1)
        collector=asyncio.create_task(self.collect(session));await session.flush()
        self.assertEqual(await collector,[])
        self.assertTrue(session.diagnostics()['finished_marker'])
        self.assertFalse(session.diagnostics()['turn_after_end'])
        await session.close()

    def setUp(self):
        self.environment=patch.dict(os.environ,GEMINI_API_KEY='synthetic-key');self.environment.start();self.addCleanup(self.environment.stop)
        self.socket=Socket();self.connections=[];self.clients=[]
        def connect(uri,**options):self.connections.append((uri,options));return self.socket
        self.transport=patch('google.genai.live.ws_connect',connect);self.transport.start();self.addCleanup(self.transport.stop)

    def request(self,settings=None,model='gemini-3.5-transcribe-live'):
        entry=ModelEntry(provider='gemini',mode='live',model_id=model,display_name='Test',
            capability_profile_id=f'gemini:{model}:live',settings=settings or TranscriptionSettings())
        snapshot=ModelSnapshot.from_entry(entry,effective=effective_settings(entry.provider,entry.settings))
        return LiveRequest(model=snapshot,context=EventContext(run_id=uuid4(),reading='D',attempt_id=uuid4(),
            model_entry_id=entry.id,segment_id=uuid4(),segment_index=0))

    def adapter(self,**options):
        def factory(**kwargs):
            client=genai.Client(**kwargs);self.clients.append(client);return client
        return GeminiLiveAdapter(client_factory=factory,allow_paid=True,**options)

    async def collect(self,session):return [event async for event in session.events()]

    async def test_installed_sdk_setup_audio_partial_replacement_final_and_flush(self):
        request=self.request(TranscriptionSettings(language_hint='lv',keywords=('Rīga','LSM'),formatting='smart'))
        session=await self.adapter().open_live(request);collect=asyncio.create_task(self.collect(session))
        pcm=b'\x01\x00'*1920
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=pcm))
        await session.flush();await session.flush();events=await collect
        self.assertEqual([event.text for event in events],['Rīg','Rīga, ābols!','Rīga, ābols!'])
        self.assertEqual([event.revision for event in events],[1,2,3]);self.assertEqual(len({e.span_id for e in events}),1)
        self.assertIsInstance(events[-1],FinalTranscriptEvent)
        setup=self.socket.sent[0]['setup'];self.assertEqual(setup['model'],'models/gemini-3.5-transcribe-live')
        self.assertEqual(setup['generationConfig']['responseModalities'],['TEXT'])
        # Installed SDK passes this nested protobuf configuration through in snake_case.
        self.assertEqual(setup['inputAudioTranscription']['language_codes'],['lv-LV'])
        self.assertEqual(setup['inputAudioTranscription']['custom_vocabulary'],['Rīga','LSM'])
        self.assertEqual(setup['inputAudioTranscription']['mode'],'SMART')
        packets=[m['realtime_input'] for m in self.socket.sent if 'realtime_input' in m]
        audio=[m['audio'] for m in packets if 'audio' in m]
        self.assertEqual([len(base64.b64decode(m['data'])) for m in audio],[3200,640])
        self.assertEqual(b''.join(base64.b64decode(m['data']) for m in audio),pcm)
        self.assertTrue(all(m['mime_type']=='audio/pcm;rate=16000' for m in audio))
        self.assertEqual(sum(m.get('audioStreamEnd',False) for m in packets),1)
        self.assertTrue(self.connections[0][0].startswith('wss://generativelanguage.googleapis.com/ws/'))
        self.assertEqual(len(self.connections),1);await session.close();await session.close();self.assertEqual(self.socket.closed,1)

    async def test_manual_activity_signals_auto_language_and_verbatim(self):
        request=self.request(TranscriptionSettings(language_hint=None,turn_detection='manual',formatting='verbatim'))
        session=await self.adapter().open_live(request);collect=asyncio.create_task(self.collect(session))
        for index in range(2):await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=index,
            source_offset_ms=index*20,sample_rate=16000,pcm=b'\0\0'*320))
        await session.flush();await collect;await session.close()
        setup=self.socket.sent[0]['setup'];self.assertEqual(setup['inputAudioTranscription']['language_codes'],[])
        self.assertTrue(setup['realtimeInputConfig']['automatic_activity_detection']['disabled'])
        packets=[m['realtime_input'] for m in self.socket.sent if 'realtime_input' in m]
        self.assertEqual(sum('activityStart' in m for m in packets),1);self.assertEqual(sum('activityEnd' in m for m in packets),1)
        self.assertFalse(any('audioStreamEnd' in m for m in packets))

    async def test_receive_restarts_after_turn_and_preserves_repeated_speech(self):
        request=self.request();session=await self.adapter().open_live(request)
        for offset in ('0s','1s'):
            self.socket.put({'serverContent':{'inputTranscription':{'text':'Atkārtots','startOffset':offset}}})
            self.socket.put({'serverContent':{'turnComplete':True}})
        first=await asyncio.wait_for(session.queue.get(),1);second=await asyncio.wait_for(session.queue.get(),1)
        self.assertEqual([first.span_index,second.span_index],[0,1]);self.assertEqual(first.text,second.text)
        self.socket.put({'serverContent':{'inputTranscription':{'text':'Atkārtots','startOffset':'1s'}}})
        self.socket.put({'serverContent':{'interimInputTranscription':{'text':'Late partial','startOffset':'1s'}}})
        self.socket.put({'serverContent':{'inputTranscription':{'text':'Next','startOffset':'2s'}}})
        event=await asyncio.wait_for(session.queue.get(),1);self.assertEqual(event.text,'Next');self.assertEqual(event.span_index,2)
        await session.flush();await session.close()

    async def test_missing_final_times_out_without_promoting_interim(self):
        class Pending(Socket):
            def finish(inner):
                inner.put({'serverContent':{'interimInputTranscription':{'text':'Unconfirmed'}}})
                inner.put({'serverContent':{'turnComplete':True}})
        self.socket=Pending();request=self.request();session=await self.adapter(timeout_seconds=.2).open_live(request)
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'*320))
        with self.assertRaises(TimeoutError):await session.flush()
        event=await session.queue.get();self.assertEqual(event.text,'Unconfirmed');self.assertNotIsInstance(event,FinalTranscriptEvent)
        await session.close()

    async def test_settings_budget_model_and_audio_guards(self):
        for settings in (TranscriptionSettings(prompt='not supported'),TranscriptionSettings(latency='low'),
            TranscriptionSettings(keywords=('term',)*1001)):
            with self.assertRaises(ProviderFailure):await self.adapter().open_live(self.request(settings))
        with self.assertRaises(ProviderFailure):await self.adapter().open_live(self.request(model='gemini-3.5-transcribe'))
        with self.assertRaises(ProviderFailure):await GeminiLiveAdapter().open_live(self.request())
        self.assertFalse(self.connections)
        request=self.request();session=await self.adapter().open_live(request)
        with self.assertRaises(ProviderFailure):await session.send_audio(AudioFrame(segment_id=uuid4(),sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'))
        session.samples=16000*600
        with self.assertRaises(ProviderFailure):await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'))
        await session.close()

    async def test_runtime_error_safe_no_retry_and_cancelled_setup_cleanup(self):
        session=await self.adapter().open_live(self.request());collect=asyncio.create_task(self.collect(session))
        self.socket.put(errors.ClientError(429,{'error':{'message':'PRIVATE synthetic-key'}}))
        events=await collect;self.assertEqual(events[0].error.code,'rate_limit');self.assertNotIn('PRIVATE',events[0].model_dump_json())
        await session.close();self.assertEqual(len(self.connections),1)
        class NeverReady(Socket):
            async def send(inner,payload):inner.sent.append(json.loads(payload))
        self.socket=NeverReady();task=asyncio.create_task(self.adapter().open_live(self.request()))
        while not self.socket.sent:await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.socket.closed,1)

    async def test_late_final_after_turn_complete_and_setup_rejection(self):
        class Late(Socket):
            def finish(inner):
                inner.put({'serverContent':{'turnComplete':True}})
                inner.put({'serverContent':{'interimInputTranscription':{'text':'Draft'}}})
                inner.put({'serverContent':{'inputTranscription':{'text':'Late final','finished':True}}})
        self.socket=Late();request=self.request();session=await self.adapter().open_live(request)
        collect=asyncio.create_task(self.collect(session))
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=16000,pcm=b'\0\0'*320))
        await session.flush();events=await collect;self.assertEqual(events[-1].text,'Late final');await session.close()
        class BadSetup(Socket):
            async def send(inner,payload):
                inner.sent.append(json.loads(payload));inner.put({})
        self.socket=BadSetup()
        with self.assertRaises(ProviderFailure) as error:await self.adapter().open_live(self.request())
        self.assertEqual(error.exception.details.code,'unsupported_settings');self.assertEqual(self.socket.closed,1)

    async def test_go_away_and_failed_close_are_explicit(self):
        session=await self.adapter().open_live(self.request());collect=asyncio.create_task(self.collect(session))
        self.socket.put({'goAway':{'timeLeft':'1s'}})
        events=await collect;self.assertEqual(events[0].error.code,'disconnected');await session.close()
        class BadClose(Socket):
            async def __aexit__(inner,*args):raise RuntimeError('PRIVATE')
        self.socket=BadClose();session=await self.adapter().open_live(self.request())
        with self.assertRaises(ProviderFailure) as error:await session.close()
        self.assertNotIn('PRIVATE',str(error.exception))

    async def test_identified_duplicate_final_can_finish_flush_without_duplicate_text(self):
        class Finished(Socket):
            def finish(inner):
                inner.put({'serverContent':{'inputTranscription':{'text':'Existing final','startOffset':'0s','finished':True}}})
                inner.put({'serverContent':{'turnComplete':True}})
        self.socket=Finished();request=self.request();session=await self.adapter().open_live(request)
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,
            sample_rate=16000,pcm=b'\0\0'*1600))
        self.socket.put({'serverContent':{'inputTranscription':{'text':'Existing final','startOffset':'0s'}}})
        first=await asyncio.wait_for(session.queue.get(),1);self.assertEqual(first.text,'Existing final')
        collect=asyncio.create_task(self.collect(session));await session.flush()
        self.assertEqual(await collect,[]);self.assertEqual(session.final_count,1);await session.close()

if __name__=='__main__':unittest.main()
