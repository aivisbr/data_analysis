"""Installed ElevenLabs SDK over mock HTTP; no paid calls or real keys."""
import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import wave

import httpx
from elevenlabs.types import SpeechToTextChunkResponseModel

from app.contracts import EventContext, ModelEntry, ModelSnapshot, TranscriptionSettings
from app.providers.base import FileRequest, ProviderFailure
from app.providers.elevenlabs_file import ElevenLabsFileAdapter, normalize_response
from app.providers.settings import effective_settings


def result_body(text='Rīga, ābols! Atkārtots atkārtots.'):
    return dict(language_code='lav', language_probability=.98, text=text, words=[])


class ElevenLabsFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio.wav'
        self.audio(2400)
        environment = patch.dict(os.environ, ELEVENLABS_API_KEY='synthetic-eleven-key')
        environment.start()
        self.addCleanup(environment.stop)
        self.calls, self.clients = [], []

    def audio(self, frames):
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00\x01' * frames)

    def request(self, settings=None):
        settings = settings or TranscriptionSettings()
        entry = ModelEntry(provider='elevenlabs', model_id='scribe_v2', display_name='Scribe',
            mode='file', capability_profile_id='elevenlabs:scribe_v2:file', settings=settings)
        model = ModelSnapshot.from_entry(entry, effective=effective_settings(entry.provider, settings))
        return FileRequest(model=model, audio_path=self.path, mime_type='audio/wav',
            expected_audio_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            context=EventContext(run_id=uuid4(), reading='A', attempt_id=uuid4(), model_entry_id=entry.id,
                segment_id=uuid4(), segment_index=0))

    def adapter(self, handler=None, timeout=1, client_factory=None):
        def receive(request):
            self.calls.append(request)
            return handler(request) if handler else httpx.Response(200, json=result_body())
        def factory(**kwargs):
            client = httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(receive))
            self.clients.append(client)
            return client
        return ElevenLabsFileAdapter(timeout_seconds=timeout, http_client_factory=factory, client_factory=client_factory)

    async def test_real_sdk_multipart_settings_audio_and_provenance(self):
        request = self.request(TranscriptionSettings(language_hint='lv-LV', keywords=('Rīga', 'Āgenskalns')))
        outcome = await self.adapter().transcribe_file(request)
        self.assertEqual(outcome.text, result_body()['text'])
        self.assertEqual(outcome.detected_language, 'lav')
        self.assertEqual(outcome.detected_languages, ('lav',))
        self.assertEqual(outcome.duration_ms, 100)
        self.assertEqual(outcome.execution.audio_sha256, request.expected_audio_sha256)
        self.assertEqual(outcome.execution.sdk_version, '2.72.0')
        self.assertEqual(outcome.execution.effective_settings.language_hint, 'lv')
        self.assertEqual(outcome.execution.effective_settings.formatting, 'verbatim')
        self.assertEqual(request.model.requested_settings.language_hint, 'lv-LV')
        wire = self.calls[0]
        self.assertEqual(str(wire.url), 'https://api.elevenlabs.io/v1/speech-to-text')
        self.assertEqual(wire.method, 'POST')
        self.assertIn(self.path.read_bytes(), wire.content)
        for field in ('model_id', 'language_code', 'keyterms', 'no_verbatim', 'diarize', 'tag_audio_events', 'webhook', 'file_format'):
            self.assertIn(f'name="{field}"'.encode(), wire.content)
        self.assertIn('Āgenskalns'.encode(), wire.content)
        self.assertNotIn(b'name="source_url"', wire.content)
        self.assertTrue(self.clients[0].is_closed)

    async def test_auto_language_smart_and_empty_text(self):
        outcome = await self.adapter(lambda _: httpx.Response(200, json=result_body(''))).transcribe_file(
            self.request(TranscriptionSettings(language_hint=None, formatting='smart')))
        self.assertEqual(outcome.text, '')
        self.assertEqual(outcome.execution.effective_settings.formatting, 'smart')
        self.assertNotIn(b'name="language_code"', self.calls[0].content)
        self.assertIn(b'name="no_verbatim"\r\n\r\ntrue', self.calls[0].content)
        self.assertNotIn(b'name="keyterms"', self.calls[0].content)

    async def test_audio_events_removed_by_type_spoken_parentheses_kept(self):
        def word(text, kind, start=0, end=.1):
            return dict(text=text, type=kind, start=start, end=end, speaker_id='speaker_1', logprob=-.1)
        body = result_body('Rīga (laughter) (smiekli) atkārtots atkārtots!')
        body['words'] = [word('Rīga', 'word'), word(' ', 'spacing'), word('(laughter)', 'audio_event'),
            word(' ', 'spacing'), word('(smiekli)', 'word'), word(' atkārtots atkārtots!', 'word')]
        outcome = await self.adapter(lambda _: httpx.Response(200, json=body)).transcribe_file(self.request())
        self.assertEqual(outcome.text, 'Rīga  (smiekli) atkārtots atkārtots!')
        events = [a for a in outcome.annotations if a.type == 'audio_event']
        self.assertEqual(events[0].text, '(laughter)')
        self.assertEqual(events[0].speaker, 'speaker_1')
        self.assertEqual(events[0].end_offset, '0.1s')
        self.assertNotIn('speaker_1', outcome.text)
        self.assertEqual(len(outcome.annotations), 4)

    async def test_real_http_errors_safe_mapping_zero_retries_and_close(self):
        for status, expected, retryable in [(400, 'unsupported_settings', False), (401, 'authentication', False),
            (403, 'authentication', False), (404, 'unavailable_model', False), (422, 'unsupported_settings', False),
            (429, 'rate_limit', True), (500, 'internal', True)]:
            before = len(self.calls)
            adapter = self.adapter(lambda _: httpx.Response(status, json={'detail': 'PRIVATE synthetic-eleven-key'}))
            with self.assertRaises(ProviderFailure) as failure:
                await adapter.transcribe_file(self.request())
            self.assertEqual(failure.exception.details.code, expected)
            self.assertEqual(failure.exception.details.retryable, retryable)
            self.assertNotIn('PRIVATE', str(failure.exception))
            self.assertNotIn('synthetic-eleven-key', failure.exception.details.model_dump_json())
            self.assertEqual(len(self.calls), before + 1)
            self.assertTrue(self.clients[-1].is_closed)

    async def test_timeouts_connection_and_cancellation_close_owned_transport(self):
        for error, expected in [(httpx.ReadTimeout, 'timeout'), (httpx.ConnectError, 'disconnected')]:
            def broken(request):
                raise error('PRIVATE', request=request)
            with self.assertRaises(ProviderFailure) as failure:
                await self.adapter(broken).transcribe_file(self.request())
            self.assertEqual(failure.exception.details.code, expected)
            self.assertTrue(self.clients[-1].is_closed)
        entered = asyncio.Event()
        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        def client_factory(**kwargs):
            return SimpleNamespace(speech_to_text=SimpleNamespace(convert=pending))
        with self.assertRaises(ProviderFailure) as failure:
            await self.adapter(timeout=.05, client_factory=client_factory).transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'timeout')
        self.assertTrue(self.clients[-1].is_closed)
        entered.clear()
        task = asyncio.create_task(self.adapter(client_factory=client_factory).transcribe_file(self.request()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.clients[-1].is_closed)

    async def test_settings_model_key_hash_and_minimum_audio_guards(self):
        adapter, request = self.adapter(), self.request()
        for changes in [dict(model_id='scribe_v2_realtime'), dict(model_id='custom'), dict(provider='openai'),
                        dict(capability_profile_version=2), dict(effective_settings=TranscriptionSettings(language_hint='en'))]:
            with self.assertRaises(ProviderFailure):
                await adapter.transcribe_file(request.evolve(model=request.model.evolve(**changes)))
        for settings in [TranscriptionSettings(prompt='no'), TranscriptionSettings(latency='low'),
            TranscriptionSettings(keywords=('x' * 50,)), TranscriptionSettings(keywords=('one two three four five six',)),
            TranscriptionSettings(keywords=('bad[term]',)), TranscriptionSettings(keywords=tuple('x' for _ in range(1001)))]:
            with self.assertRaises(ProviderFailure):
                await adapter.transcribe_file(self.request(settings))
        with patch.dict(os.environ, ELEVENLABS_API_KEY=''), self.assertRaises(ProviderFailure):
            await adapter.transcribe_file(request)
        with self.assertRaises(ProviderFailure):
            await adapter.transcribe_file(request.evolve(expected_audio_sha256='0' * 64))
        self.audio(2399)
        with self.assertRaises(ProviderFailure) as failure:
            await adapter.transcribe_file(self.request())
        self.assertEqual(failure.exception.details.code, 'invalid_audio')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.clients, [])

    async def test_malformed_results_fail_without_returning_annotation_text(self):
        for body in [dict(text=None), dict(text=12), dict(transcripts=[]), dict(message='PRIVATE')]:
            with self.assertRaises(ProviderFailure):
                await self.adapter(lambda _: httpx.Response(200, json=body)).transcribe_file(self.request())
            self.assertTrue(self.clients[-1].is_closed)
        for words in [[SimpleNamespace(text='x', type='unknown')],
                      [SimpleNamespace(text='x', type='word', start=float('nan'), end=1)],
                      [SimpleNamespace(text='(event)', type='audio_event', start=None, end=None)],
                      [SimpleNamespace(text='speech mislabeled as spacing', type='spacing')]]:
            with self.assertRaises(ProviderFailure):
                normalize_response(SimpleNamespace(text='x', words=words))

    async def test_failure_does_not_cancel_an_independent_call(self):
        outcomes = await asyncio.gather(self.adapter().transcribe_file(self.request()),
            self.adapter(lambda _: httpx.Response(429, json={})).transcribe_file(self.request()), return_exceptions=True)
        self.assertEqual(outcomes[0].text, result_body()['text'])
        self.assertIsInstance(outcomes[1], ProviderFailure)
        self.assertTrue(all(client.is_closed for client in self.clients))


if __name__ == '__main__':
    unittest.main()
