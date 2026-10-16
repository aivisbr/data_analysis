"""Native SDK wire tests over in-memory HTTP, plus bounded lifecycle failure mocks."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import wave

import httpx
from google import genai
from google.genai import errors, interactions, types

from app.contracts import EventContext, ModelEntry, ModelSnapshot, TranscriptionSettings
from app.providers.base import FileRequest, ProviderFailure
from app.providers.gemini_file import GeminiFileAdapter, normalize_response
from app.providers.settings import effective_settings


def response(text='Rīga, ābols! Atkārtots atkārtots.', annotations=None):
    return interactions.Interaction.model_validate({'status': 'completed', 'model': 'gemini-3.5-transcribe',
        'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': text,
            'annotations': annotations or []}]}]})


class GeminiFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio.wav'
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00\x01' * 2400)
        environment = patch.dict(os.environ, GEMINI_API_KEY='synthetic-gemini-key')
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, settings=None):
        settings = settings or TranscriptionSettings()
        entry = ModelEntry(provider='gemini', model_id='gemini-3.5-transcribe', display_name='Gemini',
            mode='file', capability_profile_id='gemini:gemini-3.5-transcribe:file', settings=settings)
        snapshot = ModelSnapshot.from_entry(entry, effective=effective_settings(entry.provider, settings))
        return FileRequest(model=snapshot, audio_path=self.path, mime_type='audio/wav',
            expected_audio_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            context=EventContext(run_id=uuid4(), reading='D', attempt_id=uuid4(), model_entry_id=entry.id,
                segment_id=uuid4(), segment_index=0))

    def mock(self, *, timeout=1, cleanup_timeout=.05):
        files = SimpleNamespace(upload=AsyncMock(return_value=types.File(name='files/test',
            uri='https://generativelanguage.googleapis.com/v1beta/files/test', state='ACTIVE')),
            get=AsyncMock(), delete=AsyncMock())
        api = SimpleNamespace(files=files, interactions=SimpleNamespace(create=AsyncMock(return_value=response()),
            sdk_configuration=SimpleNamespace(retry_config='SDK default')),
            aclose=AsyncMock())
        close_calls, constructions = [], []
        client = SimpleNamespace(aio=api, close=lambda: close_calls.append(True))
        def factory(**kwargs):
            constructions.append(kwargs)
            return client
        adapter = GeminiFileAdapter(timeout_seconds=timeout, cleanup_timeout_seconds=cleanup_timeout,
            client_factory=factory)
        return adapter, api, close_calls, constructions

    async def test_installed_sdk_resumable_upload_interaction_cleanup_wire(self):
        calls, transport_clients = [], []
        statuses = [200]
        def receive(request):
            calls.append(request)
            if request.url.path.startswith('/upload/'):
                return httpx.Response(200, json={}, headers={
                    'x-goog-upload-url': 'https://generativelanguage.googleapis.com/mock-upload'})
            if request.url.path == '/mock-upload':
                self.assertEqual(request.content, self.path.read_bytes())
                return httpx.Response(200, headers={'x-goog-upload-status': 'final'}, json={'file': {'name': 'files/test', 'mimeType': 'audio/wav',
                    'uri': 'https://generativelanguage.googleapis.com/v1beta/files/test', 'state': 'ACTIVE'}})
            if request.url.path.endswith('/interactions'):
                if statuses[0] != 200:
                    return httpx.Response(statuses[0], json={'error': {'message': 'PRIVATE synthetic-gemini-key'}})
                return httpx.Response(200, json=response().model_dump(mode='json'))
            if request.method == 'DELETE':
                return httpx.Response(200, json={})
            self.fail(f'Unexpected mock request: {request.method} {request.url.path}')
        def factory(**kwargs):
            transport = httpx.AsyncClient(transport=httpx.MockTransport(receive))
            transport_clients.append(transport)
            self.addAsyncCleanup(transport.aclose)
            return genai.Client(**{**kwargs, 'http_options': {**kwargs['http_options'], 'httpx_async_client': transport}})
        adapter = GeminiFileAdapter(client_factory=factory)
        result = await adapter.transcribe_file(self.request(TranscriptionSettings(language_hint='lv',
            keywords=('Rīga', 'Āgenskalns'), formatting='verbatim')))
        self.assertEqual(result.text, 'Rīga, ābols! Atkārtots atkārtots.')
        self.assertEqual(result.execution.sdk_version, genai.__version__)
        self.assertEqual(result.execution.provider, 'gemini')
        self.assertEqual(result.execution.audio_sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result.duration_ms, 100)
        interaction_request = next(r for r in calls if r.url.path.endswith('/interactions'))
        body = json.loads(interaction_request.content)
        self.assertEqual(body['model'], 'gemini-3.5-transcribe')
        self.assertEqual(body['generation_config']['transcription_config'], {
            'mode': {'type': 'verbatim'}, 'language_codes': ['lv-LV'], 'custom_vocabulary': ['Rīga', 'Āgenskalns']})
        self.assertFalse(body['store'])
        self.assertEqual(calls[-1].method, 'DELETE')
        self.assertEqual(len(calls), 4)
        self.assertEqual(result.cleanup_errors, ())
        statuses[0] = 429
        with self.assertRaises(ProviderFailure) as failure:
            await adapter.transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'rate_limit')
        self.assertEqual(len([r for r in calls if r.url.path.endswith('/interactions')]), 2)
        self.assertEqual(calls[-1].method, 'DELETE')
        # SDK deliberately leaves caller-injected HTTP clients owned by the caller.
        for transport in transport_clients:
            await transport.aclose()
            self.assertTrue(transport.is_closed)

    async def test_formatting_auto_language_and_exact_uploaded_bytes(self):
        for formatting in ('default', 'verbatim', 'smart'):
            adapter, api, closed, construction = self.mock()
            result = await adapter.transcribe_file(self.request(TranscriptionSettings(language_hint=None, formatting=formatting)))
            config = api.interactions.create.call_args.kwargs['generation_config']['transcription_config']
            self.assertEqual(config['mode'], 'smart' if formatting == 'smart' else {'type': 'verbatim'})
            self.assertNotIn('language_codes', config)
            self.assertEqual(api.files.upload.call_args.kwargs['file'].getvalue(), self.path.read_bytes())
            self.assertEqual(construction[0]['http_options']['retry_options']['attempts'], 1)
            self.assertIsNone(api.interactions.sdk_configuration.retry_config)
            self.assertEqual(closed, [True])
            self.assertEqual(result.detected_languages, ())
            self.assertEqual(result.execution.effective_settings.formatting,
                             'smart' if formatting == 'smart' else 'verbatim')

    async def test_annotations_separate_from_text_and_nonspoken_blocks_ignored(self):
        data = response('Rīga!', [{'type': 'word_info', 'text': 'Rīga', 'speaker': 'spk_1',
            'start_offset': '0.1s', 'end_offset': '0.5s', 'start_index': 0, 'end_index': 5},
            {'type': 'speech_metadata', 'speaker': 'spk_1', 'start_index': 0, 'end_index': 6}])
        adapter, api, _, _ = self.mock()
        api.interactions.create.return_value = data
        result = await adapter.transcribe_file(self.request())
        self.assertEqual(result.text, 'Rīga!')
        self.assertEqual(len(result.annotations), 2)
        self.assertEqual(result.annotations[0].start_offset, '0.1s')
        self.assertEqual(result.annotations[0].end_index, 5)
        self.assertNotIn('spk_1', result.text)
        self.assertEqual(normalize_response(response(''))[0], '')
        for status in ('failed', 'in_progress'):
            with self.assertRaises(ProviderFailure):
                normalize_response(SimpleNamespace(status=status))
        with self.assertRaises(ProviderFailure):
            normalize_response(SimpleNamespace(status='completed', steps=[]))

    async def test_processing_poll_and_failed_upload_state(self):
        adapter, api, _, _ = self.mock()
        api.files.upload.return_value = types.File(name='files/test', uri='https://example.test/file', state='PROCESSING')
        api.files.get.return_value = types.File(name='files/test', uri='https://example.test/file', state='ACTIVE')
        await adapter.transcribe_file(self.request())
        api.files.get.assert_awaited_once_with(name='files/test')
        adapter, api, _, _ = self.mock()
        api.files.upload.return_value = types.File(name='files/test', uri='https://example.test/file', state='FAILED')
        with self.assertRaises(ProviderFailure) as failure:
            await adapter.transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'invalid_audio')
        api.files.delete.assert_awaited_once()
        api.interactions.create.assert_not_awaited()

    async def test_error_codes_redaction_cleanup_and_no_retry(self):
        for status, code in [(400, 'unsupported_settings'), (401, 'authentication'), (403, 'authentication'),
                             (404, 'unavailable_model'), (429, 'rate_limit'), (500, 'internal')]:
            adapter, api, closed, _ = self.mock()
            api.interactions.create.side_effect = errors.APIError(status, {'error': {'message': 'PRIVATE synthetic-gemini-key'}})
            with self.assertRaises(ProviderFailure) as failure:
                await adapter.transcribe_file(self.request())
            self.assertEqual(failure.exception.details.code, code)
            self.assertNotIn('PRIVATE', str(failure.exception))
            self.assertNotIn('synthetic-gemini-key', failure.exception.details.model_dump_json())
            self.assertEqual(api.interactions.create.await_count, 1)
            api.files.delete.assert_awaited_once()
            self.assertEqual(closed, [True])

    async def test_upload_failure_timeout_and_cancellation(self):
        adapter, api, closed, _ = self.mock()
        api.files.upload.side_effect = errors.APIError(429, {'error': {'message': 'PRIVATE'}})
        with self.assertRaises(ProviderFailure):
            await adapter.transcribe_file(self.request())
        api.files.delete.assert_not_awaited()
        self.assertEqual(closed, [True])
        async def wait_forever(**kwargs):
            await asyncio.Event().wait()
        adapter, api, _, _ = self.mock(timeout=.05)
        api.interactions.create.side_effect = wait_forever
        with self.assertRaises(ProviderFailure) as failure:
            await adapter.transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'timeout')
        api.files.delete.assert_awaited_once()
        api.aclose.assert_awaited_once()
        adapter, api, closed, _ = self.mock()
        entered = asyncio.Event()
        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        api.interactions.create.side_effect = pending
        task = asyncio.create_task(adapter.transcribe_file(self.request()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        api.files.delete.assert_awaited_once()
        self.assertEqual(closed, [True])

    async def test_cleanup_failure_preserves_text_and_primary_error(self):
        adapter, api, _, _ = self.mock()
        api.files.delete.side_effect = RuntimeError('PRIVATE')
        result = await adapter.transcribe_file(self.request())
        self.assertTrue(result.text)
        self.assertEqual(len(result.cleanup_errors), 1)
        self.assertNotIn('PRIVATE', result.cleanup_errors[0].message)
        api.interactions.create.side_effect = errors.APIError(429, {})
        with self.assertRaises(ProviderFailure) as failure:
            await adapter.transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'rate_limit')
        self.assertEqual(len(failure.exception.cleanup_errors), 1)

    async def test_invalid_model_settings_key_hash_prevent_upload(self):
        adapter, api, _, construction = self.mock()
        request = self.request()
        for changes in [dict(model_id='gemini-3.5-transcribe-live'), dict(model_id='custom'),
                        dict(provider='openai'), dict(capability_profile_version=2)]:
            with self.assertRaises(ProviderFailure):
                await adapter.transcribe_file(request.evolve(model=request.model.evolve(**changes)))
        for settings in [TranscriptionSettings(prompt='no'), TranscriptionSettings(latency='low'),
                         TranscriptionSettings(keywords=tuple('x' for _ in range(1001)))]:
            with self.assertRaises(ProviderFailure):
                await adapter.transcribe_file(self.request(settings))
        with patch.dict(os.environ, GEMINI_API_KEY=''), self.assertRaises(ProviderFailure):
            await adapter.transcribe_file(request)
        with self.assertRaises(ProviderFailure):
            await adapter.transcribe_file(request.evolve(expected_audio_sha256='0' * 64))
        self.assertEqual(construction, [])
        api.files.upload.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
