"""Mistral SDK requests and Voxtral 3 sessions, injected transports only."""
import asyncio
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

import httpx2
from mistralai.client import Mistral

from app.contracts import (EventContext, Mode, ModelEntry, ModelSnapshot, Provider,
                           TranscriptionSettings, FinalTranscriptEvent)
from app.providers.base import AudioFrame, FileRequest, LiveRequest, ProviderFailure
from app.providers.mistral_common import FILE_MODELS, LIVE_MODEL
from app.providers.mistral_file import MistralFileAdapter
from app.providers.mistral_live import MistralLiveAdapter
from app.providers.settings import effective_settings
from app.registry import ModelRegistry


def request(mode, *, model_id=None, settings=None, segment_index=0):
    model_id = model_id or (LIVE_MODEL if mode == 'live' else FILE_MODELS[0])
    settings = settings or TranscriptionSettings()
    entry = ModelEntry(provider='mistral', model_id=model_id, display_name=model_id,
        mode=mode, capability_profile_id=f'mistral:{model_id}:{mode}', settings=settings)
    model = ModelSnapshot.from_entry(entry, effective=effective_settings(Provider.MISTRAL, settings))
    context = EventContext(run_id=uuid4(), reading='A', attempt_id=uuid4(), model_entry_id=entry.id,
        segment_id=uuid4(), segment_index=segment_index)
    if mode == 'live':
        return LiveRequest(context=context, model=model)
    return FileRequest(context=context, model=model, audio_path='audio.wav', mime_type='audio/wav')


class FileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio.wav'
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x01\x00' * 2400)
        self.environment = patch.dict(os.environ, MISTRAL_API_KEY='synthetic-key')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.calls, self.clients, self.options = [], [], []

    def adapter(self, status=200):
        def handler(req):
            self.calls.append(req)
            return httpx2.Response(status, json={'model': FILE_MODELS[0], 'text': 'Rīga! Atkal atkal.',
                'language': 'lv', 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})
        def factory(**kwargs):
            self.options.append(kwargs)
            sync = httpx2.Client(transport=httpx2.MockTransport(handler))
            async_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
            self.clients.extend((sync, async_client))
            # Explicitly supplied clients are owned by this test; close them in cleanup.
            client = Mistral(**kwargs, client=sync, async_client=async_client)
            self.addCleanup(sync.close)
            self.addAsyncCleanup(async_client.aclose)
            return client
        return MistralFileAdapter(client_factory=factory, timeout_seconds=2)

    def req(self, **options):
        return request('file', **options).evolve(audio_path=self.path,
            expected_audio_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())

    async def test_both_exact_models_native_multipart_and_provenance(self):
        for model_id in FILE_MODELS:
            result = await self.adapter().transcribe_file(self.req(model_id=model_id,
                settings=TranscriptionSettings(language_hint='lv-LV', keywords=('Edvards Virza',))))
            self.assertEqual(result.text, 'Rīga! Atkal atkal.')
            self.assertEqual(result.execution.model_id, model_id)
            self.assertEqual(result.execution.automatic_retries, 0)
            self.assertEqual(result.execution.effective_settings.language_hint, 'lv')
            body = self.calls[-1].content
            self.assertIn(model_id.encode(), body)
            self.assertIn(b'Edvards Virza', body)
            self.assertIn(self.path.read_bytes(), body)
            self.assertEqual(str(self.calls[-1].url), 'https://api.mistral.ai/v1/audio/transcriptions')
            self.assertIsNone(self.options[-1]['retry_config'])

    async def test_429_is_one_request_safe_error(self):
        with self.assertRaises(ProviderFailure) as caught:
            await self.adapter(429).transcribe_file(self.req())
        self.assertEqual(caught.exception.details.code, 'rate_limit')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('synthetic-key', str(caught.exception))

    async def test_settings_hash_and_missing_key_fail_before_network(self):
        adapter = self.adapter()
        for req in (self.req().evolve(expected_audio_sha256='0' * 64),
                    self.req(settings=TranscriptionSettings(prompt='unsupported')),
                    self.req(model_id='voxtral-mini-latest')):
            with self.assertRaises(ProviderFailure):
                await adapter.transcribe_file(req)
        with patch.dict(os.environ, MISTRAL_API_KEY=''), self.assertRaises(ProviderFailure):
            await adapter.transcribe_file(self.req())
        self.assertEqual(self.calls, [])


class Socket:
    def __init__(self, *, final=True, disconnect=False, reject=False, stream_error=False):
        self.messages = asyncio.Queue()
        self.messages.put_nowait({'type': 'session.created'})
        self.sent, self.closed = [], False
        self.final, self.disconnect, self.reject = final, disconnect, reject
        self.stream_error = stream_error

    async def recv(self):
        value = await self.messages.get()
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def send(self, encoded):
        msg = json.loads(encoded)
        self.sent.append(msg)
        if msg['type'] == 'session.update':
            self.messages.put_nowait({'type': 'error' if self.reject else 'session.updated'})
        elif msg['type'] == 'input_audio.append':
            self.messages.put_nowait({'type': 'transcription.text.delta', 'text': 'Rīga '})
            if self.stream_error:
                self.messages.put_nowait({'type': 'error', 'error': {
                    'code': 4290, 'message': 'Rate limit reached. synthetic-key must never be stored'}})
            if self.disconnect:
                self.messages.put_nowait(None)
        elif msg['type'] == 'input_audio.end' and self.final:
            self.messages.put_nowait({'type': 'transcription.done', 'text': 'Rīga! Rīga!'})

    async def close(self):
        self.closed = True


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, MISTRAL_API_KEY='synthetic-key')
        env.start()
        self.addCleanup(env.stop)
        self.connections = []

    async def session(self, socket=None, req=None):
        socket, req = socket or Socket(), req or request('live')
        async def connect(url, **kwargs):
            self.connections.append((url, kwargs))
            return socket
        session = await MistralLiveAdapter(allow_paid=True, connect_factory=connect,
            final_timeout_seconds=.05).open_live(req)
        self.addAsyncCleanup(session.close)
        return session, socket, req

    def frame(self, req, sequence=0):
        return AudioFrame(segment_id=req.context.segment_id, sequence=sequence,
            source_offset_ms=sequence * 110, sample_rate=16000, pcm=b'\x01\x00' * 1601)

    async def test_settings_before_audio_and_authoritative_final_replaces_deltas(self):
        req = request('live', settings=TranscriptionSettings(keywords=('Rīga',), latency='high'))
        session, sock, req = await self.session(req=req)
        events = []
        async def collect():
            async for event in session.events():
                events.append(event)
        receiver = asyncio.create_task(collect())
        await session.send_audio(self.frame(req))
        await asyncio.sleep(0)
        await session.flush()
        await receiver
        self.assertEqual([m['type'] for m in sock.sent],
            ['session.update', 'input_audio.append', 'input_audio.append', 'input_audio.end'])
        self.assertEqual(sock.sent[0]['session']['language'], 'lv')
        self.assertEqual(sock.sent[0]['session']['context_bias'], ['Rīga'])
        self.assertEqual(sock.sent[0]['session']['target_streaming_delay_ms'], 1200)
        self.assertEqual(events[0].text, 'Rīga ')
        self.assertIsInstance(events[-1], FinalTranscriptEvent)
        self.assertEqual(events[-1].text, 'Rīga! Rīga!')
        self.assertEqual(session.diagnostics()['audio_samples'], 1601)
        self.assertEqual([len(base64.b64decode(m['audio'])) for m in sock.sent if m['type'] == 'input_audio.append'], [3200, 2])
        self.assertIsInstance(events[0], FinalTranscriptEvent)
        self.assertTrue(session.diagnostics()['done_received'])
        await session.flush()
        self.assertEqual(len(sock.sent), 4)

    async def test_resume_segment_accepts_global_sequence_and_isolated_text(self):
        first, _, req1 = await self.session()
        await first.send_audio(self.frame(req1))
        await first.flush()
        second, _, req2 = await self.session(req=request('live', segment_index=1))
        await second.send_audio(self.frame(req2, 50))
        await second.flush()
        self.assertEqual(second.text, 'Rīga! Rīga!')
        self.assertEqual(second.samples, 1601)
        self.assertEqual(len(self.connections), 2)

    async def test_missing_done_preserves_confirmed_prefix_but_not_complete(self):
        session, _, req = await self.session(Socket(final=False))
        await session.send_audio(self.frame(req))
        with self.assertRaises(ProviderFailure) as error:
            await session.flush()
        self.assertEqual(error.exception.details.code, 'timeout')
        self.assertFalse(session.finished)
        self.assertTrue(all(isinstance(e, FinalTranscriptEvent) for e in session.queue._queue))
        self.assertTrue(session.queue.qsize())

    async def test_disconnect_preserves_partial_and_never_reconnects(self):
        session, _, req = await self.session(Socket(disconnect=True))
        await session.send_audio(self.frame(req))
        await session.done.wait()
        self.assertEqual(session.queue.get_nowait().text, 'Rīga ')
        with self.assertRaises(ProviderFailure):
            await session.flush()
        self.assertEqual(len(self.connections), 1)

    async def test_provider_error_retains_code_category_and_confirmed_text_without_body(self):
        session, _, req = await self.session(Socket(stream_error=True))
        await session.send_audio(self.frame(req))
        await session.done.wait()
        event = session.queue.get_nowait()
        self.assertIsInstance(event, FinalTranscriptEvent)
        self.assertEqual(event.text, 'Rīga ')
        with self.assertRaises(ProviderFailure) as caught:
            await session.flush()
        self.assertEqual(caught.exception.details.code, 'rate_limit')
        protocol = session.diagnostics()
        self.assertEqual(protocol['provider_error'], {'code': '4290', 'category': 'rate_limit', 'phase': 'streaming'})
        self.assertFalse(protocol['end_sent'])
        self.assertNotIn('synthetic-key', json.dumps(protocol) + str(caught.exception))
        self.assertFalse(protocol['done_received'])

    async def test_rejected_configuration_closes_before_audio(self):
        sock = Socket(reject=True)
        with self.assertRaises(ProviderFailure):
            await self.session(sock)
        self.assertTrue(sock.closed)
        self.assertEqual([m['type'] for m in sock.sent], ['session.update'])

    async def test_paid_gate_and_invalid_audio(self):
        with self.assertRaises(ProviderFailure):
            await MistralLiveAdapter().open_live(request('live'))
        session, _, req = await self.session()
        await session.send_audio(self.frame(req))
        with self.assertRaises(ProviderFailure):
            await session.send_audio(self.frame(req))
        self.assertEqual(len(self.connections), 1)

    async def test_distribution_pause_resume_saves_both_segments_while_peer_fails(self):
        from app.live_distribution import LiveDistribution, LiveStart, FrameHeader
        from app.transcripts import TranscriptStore
        from app.contracts import ErrorCode, ErrorDetails
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = ModelRegistry(root / 'app.sqlite3')
            registry.initialize()
            for entry in registry.list(Mode.LIVE):
                registry.update(entry.id, {'enabled': entry.provider == Provider.MISTRAL or entry.model_id == 'gpt-transcribe'})
            sockets = []
            async def connect(*args, **kwargs):
                sock = Socket()
                sockets.append(sock)
                return sock
            class Failed:
                async def open_live(self, req):
                    raise ProviderFailure(ErrorDetails(code=ErrorCode.AUTHENTICATION, message='Testa kļūda.'))
            factory = lambda model: (MistralLiveAdapter(allow_paid=True, connect_factory=connect)
                                     if model.provider == Provider.MISTRAL else Failed())
            store = TranscriptStore(root / 'runs')
            pipeline = LiveDistribution(registry, store, factory=factory, execution_mode='paid', timeout=2)
            segment = uuid4()
            info = LiveStart(reading='D', segment_id=segment, sample_rate=48000, channels=1, source_start_sample=0)
            await pipeline.start(info)
            def frame(seg, seq, start, accepted):
                return FrameHeader(sequence=seq, segment_id=seg, sample_rate=48000, channels=1,
                    frame_samples=480, source_start_sample=start, source_end_sample=start + 480,
                    audio_start_sample=accepted, audio_end_sample=accepted + 480)
            pipeline.offer(frame(segment, 0, 0, 0), b'\x01\x00' * 480)
            await pipeline.pause()
            resumed = uuid4()
            await pipeline.resume(info.evolve(segment_id=resumed, source_start_sample=48000))
            pipeline.offer(frame(resumed, 1, 48000, 480), b'\x01\x00' * 480)
            result = await pipeline.stop()
            checkpoint = store.load(pipeline.run.id)
            model = next(m for m in checkpoint.run.models if m.provider == Provider.MISTRAL)
            attempt = next(a for a in checkpoint.run.readings[0].attempts if a.model_entry_id == model.entry_id)
            self.assertEqual(result['models'][str(model.entry_id)]['status'], 'complete')
            self.assertEqual(attempt.save_status, 'saved')
            self.assertEqual(len(sockets), 2)
            self.assertTrue(all(sock.closed for sock in sockets))
            self.assertEqual((store.root / str(pipeline.run.id) / attempt.raw_file).read_text(encoding='utf-8'),
                             'Rīga! Rīga! Rīga! Rīga!')
            execution = result['models'][str(model.entry_id)]['executions']
            self.assertEqual(len(execution), 2)
            self.assertTrue(all(e['protocol']['done_received'] for e in execution))
            self.assertTrue(all(e['config']['audio_format']['sample_rate'] == 16000 for e in execution))
            self.assertEqual(checkpoint.run.status, 'incomplete')  # Failed peer does not discard Mistral output.

    async def test_stream_error_exports_confirmed_prefix_as_incomplete(self):
        from app.live_distribution import LiveDistribution, LiveStart, FrameHeader
        from app.transcripts import TranscriptStore
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = ModelRegistry(root / 'app.sqlite3')
            registry.initialize()
            for entry in registry.list(Mode.LIVE):
                registry.update(entry.id, {'enabled': entry.provider == Provider.MISTRAL})
            socket = Socket(stream_error=True)
            async def connect(*args, **kwargs):
                return socket
            store = TranscriptStore(root / 'runs')
            pipeline = LiveDistribution(registry, store, execution_mode='paid', timeout=2,
                factory=lambda model: MistralLiveAdapter(allow_paid=True, connect_factory=connect))
            segment = uuid4()
            await pipeline.start(LiveStart(reading='A', segment_id=segment, sample_rate=48000,
                channels=1, source_start_sample=0))
            pipeline.offer(FrameHeader(sequence=0, segment_id=segment, sample_rate=48000, channels=1,
                frame_samples=4800, source_start_sample=0, source_end_sample=4800,
                audio_start_sample=0, audio_end_sample=4800), b'\x01\x00' * 4800)
            result = await pipeline.stop()
            checkpoint = store.load(pipeline.run.id)
            attempt = checkpoint.run.readings[0].attempts[0]
            self.assertEqual(attempt.status, 'incomplete')
            self.assertEqual(attempt.save_status, 'saved')
            for name in (attempt.raw_file, attempt.editable_file):
                self.assertEqual((store.root / str(pipeline.run.id) / name).read_text(encoding='utf-8'), 'Rīga')
            state = next(iter(result['models'].values()))
            self.assertEqual(state['error']['code'], 'rate_limit')
            self.assertFalse(state['executions'][0]['protocol']['done_received'])


class RegistryTests(unittest.TestCase):
    def test_one_time_existing_registry_upgrade_preserves_selection_and_deletions(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = ModelRegistry(Path(folder) / 'app.sqlite3')
            registry.initialize()
            original = next(m for m in registry.list() if m.provider != Provider.MISTRAL)
            registry.update(original.id, {'enabled': False})
            # Simulate a pre-Voxtral database, with the original seed marker retained.
            with closing(sqlite3.connect(registry.database)) as db, db:
                db.execute("DELETE FROM registry_metadata WHERE key='mistral_v3_seeded'")
                db.execute("DELETE FROM model_entries WHERE payload LIKE '%\"provider\":\"mistral\"%'")
            registry.initialize()
            models = [m for m in registry.list() if m.provider == Provider.MISTRAL]
            self.assertEqual(len(models), 3)
            self.assertTrue(all(not m.enabled for m in models))
            self.assertFalse(registry.get(original.id).enabled)
            for model in models:
                registry.delete(model.id)
            registry.initialize()
            self.assertFalse(any(m.provider == Provider.MISTRAL for m in registry.list()))

    def test_routing_is_exact_and_new_models_are_not_selected_implicitly(self):
        from app.file_jobs import adapter_for
        from app.providers.live_factory import paid_live_factory
        self.assertIsInstance(adapter_for(Provider.MISTRAL), MistralFileAdapter)
        self.assertIsInstance(paid_live_factory(request('live').model), MistralLiveAdapter)
        with tempfile.TemporaryDirectory() as folder:
            registry = ModelRegistry(Path(folder) / 'app.sqlite3')
            registry.initialize()
            for mode in Mode:
                self.assertFalse(any(m.provider == Provider.MISTRAL for m in registry.create_run(mode).models))
