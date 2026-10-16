import asyncio
import base64
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

from app.contracts import EventContext, ModelEntry, ModelSnapshot, TranscriptionSettings, FinalTranscriptEvent
from app.providers.base import AudioFrame, LiveRequest, ProviderFailure
from app.providers.settings import effective_settings
from app.providers.elevenlabs_live import ElevenLabsLiveAdapter


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = 0
        self.finish = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    def put(self, kind, **data):
        self.queue.put_nowait(dict(message_type=kind, **data))

    async def send(self, payload):
        data = json.loads(payload)
        self.sent.append(data)
        if data['commit'] and self.finish:
            self.put('partial_transcript', text='Rīg')
            self.put('partial_transcript', text='Rīga!')
            self.put('committed_transcript', text='Rīga!')
            self.put('committed_transcript', text='Rīga!')
            self.put('committed_transcript_with_timestamps', text='Rīga!', words=[])

    async def close(self, *args):
        self.closed += 1
        self.queue.put_nowait(None)


class ElevenLabsLiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ELEVENLABS_API_KEY='synthetic-key')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.socket = Socket()
        self.connections = []
        self.bad_setup = False

        async def connect(uri, **kwargs):
            self.connections.append((uri, kwargs))
            config = dict(model_id='scribe_v2_realtime', audio_format='pcm_24000', sample_rate=24000)
            if self.bad_setup:
                config['sample_rate'] = 16000
            self.socket.put('session_started', config=config)
            return self.socket
        self.transport = patch('elevenlabs.realtime.scribe.websocket_connect', connect)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def request(self, settings=None):
        entry = ModelEntry(provider='elevenlabs', mode='live', model_id='scribe_v2_realtime', display_name='Test',
                           capability_profile_id='elevenlabs:scribe_v2_realtime:live', settings=settings or TranscriptionSettings())
        model = ModelSnapshot.from_entry(entry, effective=effective_settings(entry.provider, entry.settings))
        return LiveRequest(model=model, context=EventContext(run_id=uuid4(), reading='A', attempt_id=uuid4(),
                           model_entry_id=entry.id, segment_id=uuid4(), segment_index=0))

    def frame(self, request, sequence=0, samples=2880):
        return AudioFrame(segment_id=request.context.segment_id, sequence=sequence, source_offset_ms=sequence*120,
                          sample_rate=24000, pcm=b'\x01\x00'*samples)

    async def collect(self, session):
        return [event async for event in session.events()]

    async def test_native_sdk_wire_partial_final_duplicate_and_exact_tail(self):
        request = self.request(TranscriptionSettings(language_hint='lv-LV', keywords=('Rīga',), formatting='smart'))
        session = await ElevenLabsLiveAdapter(allow_paid=True).open_live(request)
        collector = asyncio.create_task(self.collect(session))
        frame = self.frame(request)
        await session.send_audio(frame)
        await session.flush()
        await session.flush()
        events = await collector
        self.assertEqual([e.text for e in events], ['Rīg', 'Rīga!', 'Rīga!'])
        self.assertEqual([e.revision for e in events], [1, 2, 3])
        self.assertEqual(len({e.span_id for e in events}), 1)
        self.assertIsInstance(events[-1], FinalTranscriptEvent)
        self.assertEqual(b''.join(base64.b64decode(d['audio_base_64']) for d in self.socket.sent), frame.pcm)
        self.assertEqual(sum(d['commit'] for d in self.socket.sent), 1)
        uri, headers = self.connections[0]
        self.assertEqual(urlparse(uri).netloc, 'api.elevenlabs.io')
        params = parse_qs(urlparse(uri).query)
        self.assertEqual(params['language_code'], ['lv'])
        self.assertEqual(params['keyterms'], ['Rīga'])
        self.assertEqual(params['no_verbatim'], ['true'])
        self.assertEqual(params['commit_strategy'], ['manual'])
        self.assertNotIn('synthetic-key', uri)
        self.assertEqual(headers['additional_headers'], {'xi-api-key': 'synthetic-key'})
        await session.close()
        await session.close()
        self.assertEqual(self.socket.closed, 1)

    async def test_repeated_speech_across_commits_is_preserved(self):
        request = self.request()
        session = await ElevenLabsLiveAdapter(allow_paid=True).open_live(request)
        collector = asyncio.create_task(self.collect(session))
        for i in range(20):
            await session.send_audio(self.frame(request, i, 24000))
        await session.send_audio(self.frame(request, 20))
        await session.flush()
        events = await collector
        finals = [e for e in events if isinstance(e, FinalTranscriptEvent)]
        self.assertEqual([e.text for e in finals], ['Rīga!', 'Rīga!'])
        self.assertEqual([e.span_index for e in finals], [0, 1])
        await session.close()

    async def test_guard_budget_language_vocabulary_and_unsupported_settings(self):
        with self.assertRaises(ProviderFailure):
            await ElevenLabsLiveAdapter().open_live(self.request())
        for settings in (TranscriptionSettings(turn_detection='automatic'), TranscriptionSettings(prompt='secret'),
                         TranscriptionSettings(keywords=('x'*21,)), TranscriptionSettings(keywords=tuple(str(i) for i in range(51)))):
            with self.assertRaises(ProviderFailure):
                await ElevenLabsLiveAdapter(allow_paid=True).open_live(self.request(settings))
        self.assertEqual(self.connections, [])

    async def test_missing_final_timeout_does_not_promote_partial(self):
        request = self.request()
        session = await ElevenLabsLiveAdapter(allow_paid=True, timeout_seconds=.1).open_live(request)
        self.socket.finish = False
        await session.send_audio(self.frame(request))
        self.socket.put('partial_transcript', text='Nepabeigts')
        with self.assertRaises(ProviderFailure) as caught:
            await session.flush()
        self.assertEqual(caught.exception.details.code, 'timeout')
        await session.close()
        events = await self.collect(session)
        self.assertFalse(any(isinstance(e, FinalTranscriptEvent) for e in events))

    async def test_runtime_error_is_safe_and_not_retried(self):
        session = await ElevenLabsLiveAdapter(allow_paid=True).open_live(self.request())
        self.socket.put('rate_limited', error='synthetic-key sensitive-response')
        await asyncio.sleep(.01)
        with self.assertRaises(ProviderFailure) as caught:
            await session.flush()
        self.assertEqual(caught.exception.details.code, 'rate_limit')
        self.assertNotIn('sensitive', str(caught.exception))
        self.assertEqual(len(self.connections), 1)
        await session.close()

    async def test_bad_setup_and_wrong_audio_are_rejected(self):
        self.bad_setup = True
        with self.assertRaises(ProviderFailure):
            await ElevenLabsLiveAdapter(allow_paid=True).open_live(self.request())
        self.assertEqual(self.socket.closed, 1)
        self.bad_setup = False
        self.socket = Socket()
        request = self.request()
        session = await ElevenLabsLiveAdapter(allow_paid=True).open_live(request)
        wrong = AudioFrame(segment_id=uuid4(), sequence=0, source_offset_ms=0, sample_rate=16000, pcm=b'\0\0')
        with self.assertRaises(ProviderFailure):
            await session.send_audio(wrong)
        await session.flush()  # Empty session: no artificial audio or commit.
        self.assertEqual(self.socket.sent, [])
        await session.close()

    async def test_setup_cancellation_closes_owned_connection(self):
        async def connect(*args, **kwargs):
            return self.socket
        with patch('elevenlabs.realtime.scribe.websocket_connect', connect):
            task = asyncio.create_task(ElevenLabsLiveAdapter(allow_paid=True).open_live(self.request()))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.socket.closed, 1)

    async def test_close_timeout_cancels_sdk_receiver_and_unblocks_events(self):
        session = await ElevenLabsLiveAdapter(allow_paid=True, timeout_seconds=.1).open_live(self.request())
        collector = asyncio.create_task(self.collect(session))
        async def stalled_close():
            await asyncio.Event().wait()
        session.raw.close = stalled_close
        with self.assertRaises(ProviderFailure) as caught:
            await session.close()
        self.assertEqual(caught.exception.details.code, 'timeout')
        self.assertTrue(session.raw._message_task.done())
        self.assertEqual(await asyncio.wait_for(collector, 1), [])

    async def test_overflow_and_disconnect_fail_without_raw_error_text(self):
        request = self.request()
        session = await ElevenLabsLiveAdapter(allow_paid=True).open_live(request)
        await session.send_audio(self.frame(request))
        for i in range(257):
            self.socket.put('partial_transcript', text=str(i))
        await asyncio.sleep(.01)
        self.assertEqual(session.error.code, 'queue_overflow')
        await session.close()
        self.socket = Socket()
        session = await ElevenLabsLiveAdapter(allow_paid=True).open_live(self.request())
        self.socket.queue.put_nowait(None)
        await asyncio.sleep(.01)
        self.assertEqual(session.error.code, 'disconnected')
        await session.close()
