import asyncio
import base64
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import openai
from app.contracts import EventContext, ModelEntry, ModelSnapshot, TranscriptionSettings, FinalTranscriptEvent
from app.providers.base import AudioFrame, LiveRequest, ProviderFailure
from app.providers.settings import effective_settings
from app.providers.openai_live import OpenAILiveAdapter, session_config


class Socket:
    def __init__(self):self.incoming=asyncio.Queue();self.sent=[];self.closed=0;self.count=0
    def put(self,event):self.incoming.put_nowait(json.dumps(event))
    async def recv(self,*args,**kwargs):return await self.incoming.get()
    async def send(self,payload):
        event=json.loads(payload);self.sent.append(event)
        if event['type']=='session.update':self.put({'type':'session.updated','event_id':'ready','session':event['session']})
        if event['type']=='input_audio_buffer.commit':
            self.count+=1;identity=f'item_{self.count}'
            self.put({'type':'input_audio_buffer.committed','event_id':f'commit_{self.count}',
                'item_id':identity,'previous_item_id':f'item_{self.count-1}' if self.count>1 else None})
            self.put({'type':'conversation.item.input_audio_transcription.completed','event_id':f'final_{self.count}',
                'item_id':identity,'content_index':0,'transcript':f'Turn {self.count}'})
    async def close(self,*args,**kwargs):self.closed+=1


class OpenAILiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalization_deadline_is_independent_of_socket_operation_timeout(self):
        class Delayed(Socket):
            async def send(inner,payload):
                event=json.loads(payload)
                if event['type']!='input_audio_buffer.commit':return await super(Delayed,inner).send(payload)
                inner.sent.append(event)
                inner.put({'type':'input_audio_buffer.committed','event_id':'ack','item_id':'one','previous_item_id':None})
                inner.put({'type':'conversation.item.input_audio_transcription.delta','event_id':'part','item_id':'one','content_index':0,'delta':'Pending'})
                asyncio.get_running_loop().call_later(.2,inner.put,{'type':'conversation.item.input_audio_transcription.completed','event_id':'done','item_id':'one','content_index':0,'transcript':'Confirmed'})
        self.socket=Delayed();request=self.request('gpt-transcribe')
        session=await self.adapter(timeout_seconds=.1,final_timeout_seconds=.7).open_live(request)
        collector=asyncio.create_task(self.collect(session))
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=24000,pcm=b'\0\0'*2400))
        await session.flush();events=await collector
        self.assertEqual(events[-1].text,'Confirmed')
        self.assertIsInstance(events[-1],FinalTranscriptEvent)
        diagnostic=session.diagnostics()
        self.assertEqual((diagnostic['commits_sent'],diagnostic['commit_acknowledgements'],diagnostic['final_items']),(1,1,1))
        self.assertNotIn('Pending',json.dumps(diagnostic))
        self.assertNotIn('synthetic-key',json.dumps(diagnostic))
        await session.close()

    def setUp(self):
        self.environment=patch.dict(os.environ,OPENAI_API_KEY='synthetic-key');self.environment.start();self.addCleanup(self.environment.stop)
        self.socket=Socket();self.connections=[];self.clients=[]
        async def connect(url,**options):self.connections.append((url,options));return self.socket
        self.transport=patch('openai.lib._websocket._WebSocketConnect',connect);self.transport.start();self.addCleanup(self.transport.stop)

    def request(self,model='gpt-live-transcribe',settings=None):
        entry=ModelEntry(provider='openai',mode='live',model_id=model,display_name='Test',
            capability_profile_id=f'openai:{model}:live',settings=settings or TranscriptionSettings())
        snapshot=ModelSnapshot.from_entry(entry,effective=effective_settings(entry.provider,entry.settings))
        return LiveRequest(model=snapshot,context=EventContext(run_id=uuid4(),reading='A',attempt_id=uuid4(),
            model_entry_id=entry.id,segment_id=uuid4(),segment_index=0))

    def adapter(self,**options):
        def factory(**kwargs):
            client=openai.AsyncOpenAI(**kwargs);self.clients.append(client);return client
        return OpenAILiveAdapter(client_factory=factory,allow_paid=True,**options)

    async def test_installed_sdk_ga_wire_all_models_padding_and_flush(self):
        for model in ('gpt-transcribe','gpt-live-transcribe','gpt-realtime-whisper'):
            with self.subTest(model=model):
                self.socket=Socket();request=self.request(model)
                settings=TranscriptionSettings(latency='low') if model=='gpt-realtime-whisper' else TranscriptionSettings(keywords=('Rīga',),prompt='Diktāts')
                request=self.request(model,settings)
                session=await self.adapter().open_live(request)
                collect=asyncio.create_task(self.collect(session))
                await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,
                    sample_rate=24000,pcm=b'\x01\x00'*480))
                await session.flush();await session.flush();events=await collect
                self.assertEqual([event.text for event in events],['Turn 1']);self.assertIsInstance(events[0],FinalTranscriptEvent)
                self.assertEqual(session.padding_samples,1920)
                messages=self.socket.sent
                self.assertEqual(sum(e['type']=='input_audio_buffer.commit' for e in messages),1)
                transcription=messages[0]['session']['audio']['input']['transcription']
                self.assertEqual(transcription['model'],model)
                self.assertEqual(transcription.get('language') if model=='gpt-realtime-whisper' else transcription['languages'][0],'lv')
                audio=b''.join(base64.b64decode(e['audio']) for e in messages if e['type']=='input_audio_buffer.append')
                self.assertEqual(audio,b'\x01\x00'*480+b'\0\0'*1920)
                url,options=self.connections[-1]
                self.assertEqual(url,'wss://api.openai.com/v1/realtime?intent=transcription')
                self.assertNotIn('openai-beta',{key.lower() for key in options['additional_headers']})
                self.assertEqual(session.connection._max_retries,0)
                await session.close();await session.close();self.assertEqual(self.socket.closed,1)
                self.assertTrue(self.clients[-1].is_closed())

    async def collect(self,session):return [event async for event in session.events()]

    async def test_commit_chain_orders_out_of_order_finals_and_deduplicates_deltas(self):
        class Reordered(Socket):
            async def send(inner,payload):
                event=json.loads(payload)
                if event['type']!='input_audio_buffer.commit':return await super(Reordered,inner).send(payload)
                inner.sent.append(event);inner.count+=1
                if inner.count==2:
                    for item in [
                        {'type':'conversation.item.input_audio_transcription.completed','event_id':'f2','item_id':'two','content_index':0,'transcript':'Second'},
                        {'type':'input_audio_buffer.committed','event_id':'c2','item_id':'two','previous_item_id':'one'},
                        {'type':'input_audio_buffer.committed','event_id':'c1','item_id':'one','previous_item_id':None},
                        {'type':'conversation.item.input_audio_transcription.delta','event_id':'d1','item_id':'one','content_index':0,'delta':'Fir'},
                        {'type':'conversation.item.input_audio_transcription.delta','event_id':'d1','item_id':'one','content_index':0,'delta':'Fir'},
                        {'type':'conversation.item.input_audio_transcription.delta','event_id':'d2','item_id':'one','content_index':0,'delta':'st'},
                        {'type':'conversation.item.input_audio_transcription.completed','event_id':'f1','item_id':'one','content_index':0,'transcript':'First'}]:inner.put(item)
        self.socket=Reordered();request=self.request();session=await self.adapter(commit_seconds=.1).open_live(request)
        collect=asyncio.create_task(self.collect(session))
        for index in range(2):await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=index,
            source_offset_ms=index*100,sample_rate=24000,pcm=b'\0\0'*2400))
        await session.flush();events=await collect;await session.close()
        finals=sorted((e for e in events if isinstance(e,FinalTranscriptEvent)),key=lambda e:e.span_index)
        self.assertEqual([e.text for e in finals],['First','Second'])
        self.assertEqual([e.text for e in events if e.span_id=='one'],['Fir','First','First'])
        self.assertEqual([e.revision for e in events if e.span_id=='one'],[1,2,3])

    async def test_settings_budget_and_audio_guards_prevent_calls(self):
        for model,settings in [('gpt-live-transcribe',TranscriptionSettings(latency='low')),
            ('gpt-realtime-whisper',TranscriptionSettings(prompt='Unsupported')),
            ('gpt-transcribe',TranscriptionSettings(turn_detection='automatic')),
            ('gpt-live-transcribe',TranscriptionSettings(keywords=('<bad>',)))]:
            with self.assertRaises(ProviderFailure):await self.adapter().open_live(self.request(model,settings))
        with self.assertRaises(ProviderFailure):await OpenAILiveAdapter().open_live(self.request())
        self.assertFalse(self.connections)
        request=self.request();session=await self.adapter().open_live(request)
        with self.assertRaises(ProviderFailure):await session.send_audio(AudioFrame(segment_id=uuid4(),sequence=0,
            source_offset_ms=0,sample_rate=24000,pcm=b'\0\0'))
        self.assertEqual(len(self.socket.sent),1);await session.flush();await session.close()

    async def test_protocol_error_is_safe_and_closes_owned_resources(self):
        class ErrorSocket(Socket):
            async def send(inner,payload):
                event=json.loads(payload);inner.sent.append(event)
                inner.put({'type':'error','event_id':'err','error':{'code':'invalid_api_key','type':'invalid_request_error',
                    'message':'PRIVATE synthetic-key'}})
        self.socket=ErrorSocket()
        with self.assertRaises(ProviderFailure) as error:await self.adapter().open_live(self.request())
        self.assertEqual(error.exception.details.code,'authentication');self.assertNotIn('PRIVATE',str(error.exception))
        self.assertEqual(self.socket.closed,1);self.assertTrue(self.clients[-1].is_closed())

    async def test_missing_final_times_out_no_partial_promotion(self):
        class NoFinal(Socket):
            async def send(inner,payload):
                event=json.loads(payload)
                if event['type']!='input_audio_buffer.commit':return await super(NoFinal,inner).send(payload)
                inner.put({'type':'input_audio_buffer.committed','event_id':'c','item_id':'one','previous_item_id':None})
                inner.put({'type':'conversation.item.input_audio_transcription.delta','event_id':'d','item_id':'one','content_index':0,'delta':'Pending'})
        self.socket=NoFinal();request=self.request();session=await self.adapter(timeout_seconds=.2).open_live(request)
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,sample_rate=24000,pcm=b'\0\0'*2400))
        with self.assertRaises(TimeoutError):await session.flush()
        event=await session.queue.get();self.assertEqual(event.text,'Pending');self.assertNotIsInstance(event,FinalTranscriptEvent)
        await session.close()

    async def test_runtime_error_no_retry_and_empty_buffer_no_commit(self):
        request=self.request();session=await self.adapter().open_live(request)
        collect=asyncio.create_task(self.collect(session))
        self.socket.put({'type':'error','event_id':'runtime','error':{'code':'rate_limit_exceeded',
            'type':'invalid_request_error','message':'PRIVATE synthetic-key'}})
        events=await collect
        self.assertEqual(events[0].error.code,'rate_limit');self.assertNotIn('PRIVATE',events[0].model_dump_json())
        with self.assertRaises(ProviderFailure):await session.flush()
        await session.close();self.assertEqual(len(self.connections),1)
        self.assertFalse(any(event['type']=='input_audio_buffer.commit' for event in self.socket.sent))

    async def test_cancelled_handshake_and_failed_close_release_client(self):
        class NeverReady(Socket):
            async def send(inner,payload):inner.sent.append(json.loads(payload))
        self.socket=NeverReady();task=asyncio.create_task(self.adapter().open_live(self.request()))
        while not self.socket.sent:await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.socket.closed,1);self.assertTrue(self.clients[-1].is_closed())
        class BadClose(Socket):
            async def close(inner,*args,**kwargs):raise RuntimeError('PRIVATE')
        self.socket=BadClose();session=await self.adapter().open_live(self.request())
        with self.assertRaises(ProviderFailure) as error:await session.close()
        self.assertNotIn('PRIVATE',str(error.exception));self.assertTrue(self.clients[-1].is_closed())

    async def test_deprecated_model_and_unacknowledged_settings_rejected(self):
        with self.assertRaises(ProviderFailure):await self.adapter().open_live(self.request('gpt-4o-transcribe'))
        self.assertFalse(self.connections)
        class Changed(Socket):
            async def send(inner,payload):
                event=json.loads(payload)
                if event['type']=='session.update':
                    event['session']['audio']['input']['transcription']['model']='wrong-model'
                    inner.put({'type':'session.updated','event_id':'ready','session':event['session']})
        self.socket=Changed()
        with self.assertRaises(ProviderFailure) as error:await self.adapter().open_live(self.request())
        self.assertEqual(error.exception.details.code,'unsupported_settings');self.assertEqual(self.socket.closed,1)

    async def test_streaming_partial_before_commit_uses_created_item_order(self):
        request=self.request();session=await self.adapter().open_live(request)
        self.socket.put({'type':'conversation.item.created','event_id':'created','previous_item_id':None,
            'item':{'id':'item_1','type':'message','role':'user','content':[{'type':'input_audio','transcript':None}]}})
        self.socket.put({'type':'conversation.item.input_audio_transcription.delta','event_id':'early',
            'item_id':'item_1','content_index':0,'delta':'Early text'})
        event=await asyncio.wait_for(session.queue.get(),1)
        self.assertEqual(event.text,'Early text');self.assertEqual(event.span_index,0);self.assertEqual(session.commits,0)
        await session.send_audio(AudioFrame(segment_id=request.context.segment_id,sequence=0,source_offset_ms=0,
            sample_rate=24000,pcm=b'\0\0'*2400))
        collect=asyncio.create_task(self.collect(session));await session.flush();events=await collect
        self.assertEqual(events[0].text,'Turn 1');self.assertEqual(events[0].revision,2);await session.close()

if __name__=='__main__':unittest.main()
