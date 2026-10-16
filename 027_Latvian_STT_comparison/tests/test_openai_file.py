"""Real installed SDK over an in-memory transport; no credentials/network needed."""
import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4
import wave

import httpx2
import openai

from app.contracts import EventContext, ModelEntry, ModelSnapshot, TranscriptionSettings
from app.providers.base import FileRequest, ProviderFailure, ProviderAdapter
from app.providers.openai_file import OpenAIFileAdapter
from app.providers.settings import effective_settings


class OpenAIFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio.wav'
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00\x01' * 2400)
        self.environment = patch.dict(os.environ, OPENAI_API_KEY='synthetic-test-key')
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.calls = []
        self.clients = []
        self.construction = []

    def request(self, settings=None):
        settings = settings or TranscriptionSettings()
        entry = ModelEntry(provider='openai', model_id='gpt-transcribe', display_name='Paraugs',
            mode='file', capability_profile_id='openai:gpt-transcribe:file', settings=settings)
        snapshot = ModelSnapshot.from_entry(entry, effective=effective_settings(entry.provider, settings))
        context = EventContext(run_id=uuid4(), reading='A', attempt_id=uuid4(), model_entry_id=entry.id,
            segment_id=uuid4(), segment_index=0)
        return FileRequest(context=context, model=snapshot, audio_path=self.path, mime_type='audio/wav')

    def adapter(self, handler=None, timeout=1):
        def receive(request):
            self.calls.append(request)
            return handler(request) if handler else httpx2.Response(200, json={
                'text': 'Rīga, ābols!\nAtkārtots atkārtots.', 'languages': [{'code': 'lv'}]})
        def factory(**kwargs):
            self.construction.append(kwargs)
            client = openai.AsyncOpenAI(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(receive)))
            self.clients.append(client)
            return client
        return OpenAIFileAdapter(timeout_seconds=timeout, client_factory=factory)

    async def test_installed_sdk_multipart_preserves_text_audio_and_settings(self):
        settings = TranscriptionSettings(language_hint='lv-LV', keywords=('Rīga', 'Āgenskalns'), prompt='Latviešu diktāts.')
        request = self.request(settings)
        adapter = self.adapter()
        self.assertIsInstance(adapter, ProviderAdapter)
        result = await adapter.transcribe_file(request)
        self.assertEqual(result.text, 'Rīga, ābols!\nAtkārtots atkārtots.')
        self.assertEqual(result.detected_language, 'lv')
        self.assertEqual(result.detected_languages, ('lv',))
        self.assertEqual(result.duration_ms, 100)
        self.assertEqual(result.execution.audio_sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result.execution.effective_settings.language_hint, 'lv')
        self.assertEqual(request.model.requested_settings.language_hint, 'lv-LV')
        self.assertEqual(result.execution.automatic_retries, 0)
        self.assertEqual(result.execution.sdk_version, openai.__version__)
        sent = self.calls[0]
        self.assertEqual(str(sent.url), 'https://api.openai.com/v1/audio/transcriptions')
        self.assertEqual(sent.method, 'POST')
        body = sent.content
        self.assertIn(self.path.read_bytes(), body)
        for name in ('model', 'response_format', 'stream', 'languages[]', 'keywords[]', 'prompt'):
            self.assertIn(f'name="{name}"'.encode(), body)
        self.assertNotIn(b'name="language"', body)
        self.assertIn('Āgenskalns'.encode(), body)
        self.assertNotIn('beta', str(sent.url))
        self.assertNotIn('openai-beta', sent.headers)
        self.assertTrue(self.clients[0].is_closed())
        self.assertEqual(self.construction[0]['max_retries'], 0)

    async def test_auto_detection_omits_optional_fields_and_multilingual_is_not_guessed(self):
        adapter = self.adapter(lambda _: httpx2.Response(200, json={'text': '',
            'languages': [{'code': 'lv'}, {'code': 'en'}, {'code': 'lv'}]}))
        result = await adapter.transcribe_file(self.request(TranscriptionSettings(language_hint=None)))
        self.assertEqual(result.text, '')
        self.assertEqual(result.detected_languages, ('lv', 'en'))
        self.assertIsNone(result.detected_language)
        for name in ('language', 'languages[]', 'keywords[]', 'prompt'):
            self.assertNotIn(f'name="{name}"'.encode(), self.calls[0].content)

    async def test_http_error_mapping_no_retry_no_response_or_key_leak(self):
        for status, expected, retryable in [(401, 'authentication', False), (403, 'authentication', False),
            (404, 'unavailable_model', False), (429, 'rate_limit', True), (400, 'unsupported_settings', False),
            (422, 'unsupported_settings', False), (500, 'internal', True)]:
            with self.subTest(status=status):
                adapter = self.adapter(lambda _: httpx2.Response(status, json={
                    'error': {'message': 'synthetic-test-key PRIVATE provider body', 'type': 'invalid_request_error'}}))
                before = len(self.calls)
                with self.assertRaises(ProviderFailure) as failure:
                    await adapter.transcribe_file(self.request())
                self.assertEqual(failure.exception.details.code, expected)
                self.assertEqual(failure.exception.details.retryable, retryable)
                self.assertNotIn('PRIVATE', str(failure.exception))
                self.assertNotIn('synthetic-test-key', failure.exception.details.model_dump_json())
                self.assertEqual(len(self.calls), before + 1)
                self.assertTrue(self.clients[-1].is_closed())
                self.assertTrue(failure.exception.__suppress_context__)

    async def test_network_timeout_and_connection_failures(self):
        for error, expected in [(httpx2.ReadTimeout, 'timeout'), (httpx2.ConnectError, 'disconnected')]:
            def broken(request):
                raise error('PRIVATE synthetic-test-key', request=request)
            with self.assertRaises(ProviderFailure) as failure:
                await self.adapter(broken).transcribe_file(self.request())
            self.assertEqual(failure.exception.details.code, expected)
            self.assertTrue(self.clients[-1].is_closed())

    async def test_absolute_timeout_and_cancellation_close_client(self):
        closed = []
        started = asyncio.Event()
        class SlowClient:
            def __init__(self, **kwargs):
                self.audio = self
                self.transcriptions = self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                closed.append(True)
            async def create(self, **kwargs):
                started.set()
                await asyncio.Event().wait()
        adapter = OpenAIFileAdapter(timeout_seconds=.05, client_factory=SlowClient)
        with patch('app.providers.openai_file.asyncio.to_thread', new=AsyncMock(
                return_value=(self.path.read_bytes(),100))), self.assertRaises(ProviderFailure) as failure:
            await adapter.transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'timeout')
        self.assertEqual(len(closed), 1)
        adapter = OpenAIFileAdapter(timeout_seconds=1, client_factory=SlowClient)
        started.clear()
        task = asyncio.create_task(adapter.transcribe_file(self.request()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(closed), 2)

    async def test_model_snapshot_and_settings_guards_prevent_calls(self):
        request = self.request()
        for changes in [dict(model_id='whisper-1'), dict(model_id='gpt-4o-transcribe'),
            dict(model_id='gpt-4o-mini-transcribe-2025-12-15'), dict(model_id='custom-model'),
            dict(provider='gemini'), dict(capability_profile_version=2),
            dict(effective_settings=TranscriptionSettings(language_hint='en'))]:
            with self.assertRaises(ProviderFailure):
                await self.adapter().transcribe_file(request.evolve(model=request.model.evolve(**changes)))
        for settings in [TranscriptionSettings(keywords=('bad<term>',)), TranscriptionSettings(formatting='smart'),
                         TranscriptionSettings(turn_detection='manual'), TranscriptionSettings(latency='low')]:
            with self.assertRaises(ProviderFailure):
                await self.adapter().transcribe_file(self.request(settings))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.construction, [])

    async def test_missing_key_invalid_audio_and_malformed_response(self):
        with patch.dict(os.environ, OPENAI_API_KEY=''), self.assertRaises(ProviderFailure) as failure:
            await self.adapter().transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'authentication')
        for path in (self.path.with_name('missing.wav'), self.path):
            if path == self.path:
                path.write_bytes(b'not a WAV')
            with self.assertRaises(ProviderFailure) as failure:
                await self.adapter().transcribe_file(self.request().evolve(audio_path=path))
            self.assertEqual(failure.exception.details.code, 'invalid_audio')
        self.assertEqual(self.calls, [])
        self.setUpAudio()
        for body in ({'text': None}, {'other': 'PRIVATE'}, {'text': 12}):
            with self.assertRaises(ProviderFailure) as failure:
                await self.adapter(lambda _: httpx2.Response(200, json=body)).transcribe_file(self.request())
            self.assertEqual(failure.exception.details.code, 'internal')
            self.assertTrue(self.clients[-1].is_closed())

    def setUpAudio(self):
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00\x01' * 2400)

    async def test_one_failure_does_not_cancel_independent_adapter(self):
        good = self.adapter()
        bad = self.adapter(lambda _: httpx2.Response(429, json={'error': {'message': 'PRIVATE'}}))
        outcomes = await asyncio.gather(good.transcribe_file(self.request()), bad.transcribe_file(self.request()),
                                        return_exceptions=True)
        self.assertEqual(outcomes[0].text, 'Rīga, ābols!\nAtkārtots atkārtots.')
        self.assertIsInstance(outcomes[1], ProviderFailure)
        self.assertTrue(all(client.is_closed() for client in self.clients))

    async def test_expected_preparation_hash_and_audio_limits_prevent_submission(self):
        request = self.request().evolve(expected_audio_sha256='0' * 64)
        with self.assertRaises(ProviderFailure) as failure:
            await self.adapter().transcribe_file(request)
        self.assertEqual(failure.exception.details.code, 'invalid_audio')
        with patch('app.providers.openai_file.MAX_CHUNK_BYTES', 5), self.assertRaises(ProviderFailure):
            await self.adapter().transcribe_file(self.request())
        with patch('app.providers.openai_file.MAX_CHUNK_SECONDS', .01), self.assertRaises(ProviderFailure):
            await self.adapter().transcribe_file(self.request())
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00' * 400)
        with self.assertRaises(ProviderFailure):
            await self.adapter().transcribe_file(self.request())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.construction, [])


if __name__ == '__main__':
    unittest.main()
