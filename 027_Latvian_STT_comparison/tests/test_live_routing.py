import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from app.config import Settings, load_settings
from app.main import create_app
from app.contracts import Mode, Provider, PartialTranscriptEvent, FinalTranscriptEvent
from app.registry import ModelRegistry
from app.transcripts import TranscriptStore
from app.live_distribution import LiveDistribution, LiveStart
from app.live_history import LiveHistory
from app.providers.live_factory import paid_live_factory
from app.providers.base import ProviderFailure
from test_live_distribution import header


class Session:
    def __init__(self, request, config):
        self.request, self.config = request, config
        self.queue = asyncio.Queue()
        self.frames = []
        self.closed = False
        self.padding_samples = 11

    async def send_audio(self, frame):
        self.frames.append(frame)
        await self.queue.put(PartialTranscriptEvent(context=self.request.context, span_id='turn',
            span_index=0, revision=1, text='Rīg'))

    async def flush(self):
        await self.queue.put(FinalTranscriptEvent(context=self.request.context, span_id='turn',
            span_index=0, revision=2, text='Rīga!'))
        await self.queue.put(None)

    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None: return
            yield event

    async def close(self): self.closed = True


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_five_adapter_routes_pause_resume_failure_isolation_and_provenance(self):
        from app.providers.openai_live import OpenAILiveAdapter, session_config
        from app.providers.gemini_live import GeminiLiveAdapter, live_config as gemini_config
        from app.providers.elevenlabs_live import ElevenLabsLiveAdapter, live_config as eleven_config
        sessions = []
        calls = []
        async def opened(adapter, request):
            calls.append(request.model.model_id)
            if request.model.model_id == 'gpt-live-transcribe':
                raise RuntimeError('private response and key')
            config = session_config(request) if isinstance(adapter, OpenAILiveAdapter) else gemini_config(request) if isinstance(adapter, GeminiLiveAdapter) else eleven_config(request)
            result = Session(request, config)
            sessions.append(result)
            return result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ModelRegistry(root/'app.sqlite3');registry.initialize()
            store = TranscriptStore(root/'runs')
            with patch.object(OpenAILiveAdapter, 'open_live', opened), patch.object(GeminiLiveAdapter, 'open_live', opened), patch.object(ElevenLabsLiveAdapter, 'open_live', opened):
                pipeline = LiveDistribution(registry, store, execution_mode='paid',
                    factory=lambda model: paid_live_factory(model, elevenlabs_enabled=True))
                segment = uuid4()
                await pipeline.start(LiveStart(reading='A',segment_id=segment,sample_rate=48000,channels=2,source_start_sample=96000))
                self.assertEqual(len(calls), 5)
                self.assertFalse(any('[DEMO]' in m.display_name for m in pipeline.run.models))
                pipeline.offer(header(segment,0), b'\x01\0'*960)
                await pipeline.pause()
                resumed = uuid4()
                await pipeline.resume(LiveStart(reading='A',segment_id=resumed,sample_rate=48000,channels=2,source_start_sample=100000))
                resumed_header=header(resumed,1).evolve(source_start_sample=100000,source_end_sample=100480)
                pipeline.offer(resumed_header,b'\x01\0'*960)
                result = await pipeline.stop()
            self.assertEqual(calls.count('gpt-live-transcribe'), 1)  # No implicit retry on resume.
            self.assertEqual(result['execution_mode'], 'paid')
            self.assertEqual(sum(m['status']=='complete' for m in result['models'].values()), 4)
            self.assertNotIn('private', json.dumps(result))
            self.assertTrue(all(s.closed for s in sessions))
            for session in sessions:
                self.assertTrue(session.frames)
                self.assertEqual(session.frames[0].sample_rate,16000 if session.request.model.provider==Provider.GEMINI else 24000)
            checkpoint=store.load(pipeline.run.id)
            for state in result['models'].values():
                if state['status']=='complete':
                    self.assertEqual(len(state['executions']),2)
                    self.assertTrue(state['executions'][0]['sdk_version'])
                    self.assertEqual(state['executions'][0]['padding_samples'],11)
                    self.assertIsNotNone(state['executions'][0]['config'])
            history=LiveHistory(store).view(pipeline.run.id)
            self.assertEqual(history['execution_mode'],'paid')
            self.assertEqual(sum(a['final_text']=='Rīga! Rīga!' for a in history['attempts']),4)
            self.assertTrue(all(a.save_status=='saved' for a in checkpoint.run.readings[0].attempts))

    async def test_elevenlabs_route_stays_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ModelRegistry(Path(directory)/'app.sqlite3');registry.initialize()
            model=next(m for m in registry.create_run(Mode.LIVE).models if m.provider==Provider.ELEVENLABS)
            adapter=paid_live_factory(model)
            self.assertFalse(adapter.allow_paid)
            self.assertTrue(paid_live_factory(model,elevenlabs_enabled=True).allow_paid)
            with self.assertRaises(ProviderFailure):
                paid_live_factory(model.evolve(model_id='retired-unknown'))


class EndpointTests(unittest.TestCase):
    def test_file_flag_does_not_enable_live_and_rejected_start_creates_no_run(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(Path(directory),allow_paid_transcription=True))
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/live-config').json(),{'paid_enabled':False,'elevenlabs_enabled':False})
                with patch('app.live_api.paid_live_factory') as factory:
                    with client.websocket_connect('/api/live-ingest') as socket:
                        socket.send_json({'type':'start','execution_mode':'paid','source':{}})
                        self.assertEqual(socket.receive_json()['type'],'error')
                    factory.assert_not_called()
                self.assertEqual(list((Path(directory)/'runs').iterdir()),[])

    def test_demo_remains_fake_even_with_live_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(Path(directory),allow_paid_live_transcription=True))
            with TestClient(app) as client, patch('app.live_api.paid_live_factory') as factory:
                info=dict(reading='D',segment_id=str(uuid4()),sample_rate=48000,channels=2,source_start_sample=0)
                with client.websocket_connect('/api/live-ingest') as socket:
                    socket.send_json({'type':'start','source':info})
                    ready=socket.receive_json()
                    self.assertEqual(ready['execution_mode'],'demo')
                    socket.send_json({'type':'stop'})
                    self.assertEqual(socket.receive_json()['type'],'stopped')
                factory.assert_not_called()
                self.assertEqual(client.get('/api/live-runs/'+ready['run_id']).json()['execution_mode'],'demo')

    def test_environment_live_flags_are_explicit(self):
        with patch.dict(os.environ,{'P7_ALLOW_PAID_TRANSCRIPTION':'1'},clear=True):
            self.assertFalse(load_settings(env_file=None).allow_paid_live_transcription)
        with patch.dict(os.environ,{'P7_ALLOW_PAID_LIVE_TRANSCRIPTION':'1'},clear=True):
            settings=load_settings(env_file=None)
            self.assertTrue(settings.allow_paid_live_transcription)
            self.assertFalse(settings.allow_paid_elevenlabs_live)

    def test_paid_websocket_routes_only_after_explicit_permission(self):
        from app.live_distribution import FakeLiveAdapter
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(Path(directory),allow_paid_live_transcription=True))
            with TestClient(app) as client, patch('app.live_api.paid_live_factory',return_value=FakeLiveAdapter()) as factory:
                info=dict(reading='A',segment_id=str(uuid4()),sample_rate=48000,channels=2,source_start_sample=0)
                with client.websocket_connect('/api/live-ingest') as socket:
                    socket.send_json({'type':'start','execution_mode':'paid','source':info})
                    ready=socket.receive_json()
                    self.assertEqual(ready['execution_mode'],'paid')
                    self.assertEqual(factory.call_count,5)
                    self.assertTrue(all(not call.kwargs['elevenlabs_enabled'] for call in factory.call_args_list))
                    socket.send_json({'type':'stop'})
                    self.assertEqual(socket.receive_json()['type'],'stopped')
                view=client.get('/api/live-runs/'+ready['run_id']).json()
                self.assertEqual(view['execution_mode'],'paid')
                self.assertFalse(any('[DEMO]' in attempt['model'] for attempt in view['attempts']))
