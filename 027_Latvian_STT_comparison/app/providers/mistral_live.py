"""Voxtral 3 WebSocket with SDK message schemas; no reconnect or audio replay.

The installed SDK connection.update_session exposes only format/delay, although its
public message schemas include Voxtral 3 language/context. Use those schemas over
websockets so all settings go in ONE update before audio, without private SDK APIs.
"""
import asyncio
import base64
import json
import math
import os
import re

from app.contracts import ErrorCode, Mode, FinalTranscriptEvent
from app.providers.base import ProviderFailure
from app.providers.mistral_common import LIVE_MODEL, DELAYS, failure, safe_error, validate

RATE = 16000
PACKET_BYTES = RATE // 10 * 2  # Documented example: 100 ms of mono PCM per append.


def api_error(event):
    """Classify without retaining arbitrary provider text, audio or credentials."""
    data = event.get('error')
    data = data if isinstance(data, dict) else {}
    code = data.get('code')
    code = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else ''
    code = code if re.fullmatch(r'[0-9]{1,10}', code) else None
    message = data.get('message')
    if isinstance(message, dict):
        message = message.get('detail', '')
    message = message.lower() if isinstance(message, str) else ''
    category, local, description = 'unknown', ErrorCode.DISCONNECTED, 'Mistral tiešraides API kļūda.'
    for needles, label, error_code, text in (
        (('rate limit', 'too many requests', 'quota'), 'rate_limit', ErrorCode.RATE_LIMIT, 'Mistral pieprasījumu vai kvotas limits ir sasniegts.'),
        (('context length', 'context window', 'maximum context'), 'context_limit', ErrorCode.INVALID_AUDIO, 'Mistral sesijas konteksta limits ir pārsniegts.'),
        (('duration', 'session limit', 'session too long'), 'session_limit', ErrorCode.INVALID_AUDIO, 'Mistral sesijas ilguma limits ir sasniegts.'),
        (('session fields', 'streaming_delay', 'invalid language', 'diarization', 'extra inputs'), 'configuration', ErrorCode.UNSUPPORTED_SETTINGS, 'Mistral noraidīja sesijas iestatījumu.'),
        (('audio', 'pcm', 'sample rate'), 'audio', ErrorCode.INVALID_AUDIO, 'Mistral noraidīja audio datus.'),
        (('timeout', 'timed out'), 'timeout', ErrorCode.TIMEOUT, 'Mistral sesijas gaidīšanas laiks beidzās.'),
        (('internal', 'server error', 'service unavailable'), 'server', ErrorCode.DISCONNECTED, 'Mistral servera kļūda.'),
    ):
        if any(needle in message for needle in needles):
            category, local, description = label, error_code, text
            break
    if code:
        description += f' Kods: {code}.'
    return failure(local, description), dict(code=code, category=category)


class MistralLiveAdapter:
    def __init__(self, *, allow_paid=False, connect_factory=None, timeout_seconds=10, final_timeout_seconds=60):
        if any(not math.isfinite(t) or not 0 < t <= 120 for t in (timeout_seconds, final_timeout_seconds)):
            raise ValueError('Invalid timeout')
        self.allow_paid, self.factory = allow_paid, connect_factory
        self.timeout, self.final_timeout = timeout_seconds, final_timeout_seconds

    async def open_live(self, request):
        settings = validate(request, Mode.LIVE)
        if not self.allow_paid:
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Maksas Mistral tiešraide nav iespējota.')
        key = os.environ.get('MISTRAL_API_KEY', '').strip()
        if not key:
            raise failure(ErrorCode.AUTHENTICATION, 'Nav konfigurēta MISTRAL_API_KEY atslēga.')
        try:
            from websockets.asyncio.client import connect
            from mistralai.client.models import RealtimeTranscriptionSessionUpdateMessage, RealtimeTranscriptionSessionUpdatePayload
        except ImportError:
            raise failure(ErrorCode.INTERNAL, 'Nepieciešama mistralai[realtime] Python pakotne.') from None
        config = dict(audio_format=dict(encoding='pcm_s16le', sample_rate=RATE), diarize=False)
        if settings.language_hint:
            config['language'] = settings.language_hint
        if settings.keywords:
            config['context_bias'] = list(settings.keywords)
        if settings.latency:
            config['target_streaming_delay_ms'] = DELAYS[settings.latency]
        raw = None
        try:
            async with asyncio.timeout(self.timeout):
                # Await once, not `async for connect(...)` (which would reconnect).
                raw = await (self.factory or connect)(
                    f'wss://api.mistral.ai/v1/audio/transcriptions/realtime?model={LIVE_MODEL}',
                    additional_headers={'Authorization': f'Bearer {key}'}, open_timeout=self.timeout,
                    close_timeout=5, max_size=4 * 1024 * 1024)
                created = json.loads(await raw.recv())
                if created.get('type') == 'error':
                    raise api_error(created)[0]
                if created.get('type') != 'session.created':
                    raise failure(ErrorCode.DISCONNECTED, 'Mistral sesijas sākums nav apstiprināts.')
                message = RealtimeTranscriptionSessionUpdateMessage(
                    session=RealtimeTranscriptionSessionUpdatePayload(**config))
                await raw.send(message.model_dump_json())
                updated = json.loads(await raw.recv())
                if updated.get('type') == 'error':
                    raise api_error(updated)[0]
                if updated.get('type') != 'session.updated':
                    raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Mistral neapstiprināja sesijas iestatījumus.')
            return MistralLiveSession(request, config, raw, self.timeout, self.final_timeout)
        except BaseException as error:
            if raw is not None:
                try:
                    async with asyncio.timeout(self.timeout):
                        await raw.close()
                except Exception:
                    pass
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise safe_error(error, live=True) from None


class MistralLiveSession:
    def __init__(self, request, config, raw, timeout, final_timeout):
        self.request, self.config, self.raw = request, config, raw
        self.timeout, self.final_timeout = timeout, final_timeout
        self.queue = asyncio.Queue(256)
        self.changed, self.done = asyncio.Event(), asyncio.Event()
        self.lock = asyncio.Lock()
        self.closed = self.finished = self.flushing = False
        self.flush_sent = self.end_sent = False
        self.error = None
        self.sequence = self.offset = -1
        self.samples = self.revision = self.deltas = 0
        self.buffer = bytearray()
        self.provider_error = None
        self.text = ''
        self.task = asyncio.create_task(self._receive())

    def check(self):
        if self.error:
            raise ProviderFailure(self.error)

    def _emit(self, text):
        self.revision += 1
        event = FinalTranscriptEvent(context=self.request.context,
            span_id='session', span_index=0, revision=self.revision, text=text)
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            raise failure(ErrorCode.QUEUE_OVERFLOW, 'Mistral teksta notikumu rinda ir pārpildīta.') from None
        self.changed.set()

    async def _receive(self):
        try:
            async for encoded in self.raw:
                event = json.loads(encoded)
                kind = event.get('type')
                if kind == 'transcription.text.delta':
                    text = event.get('text')
                    if not isinstance(text, str) or len(self.text) + len(text) > 1_000_000:
                        raise failure(ErrorCode.INTERNAL, 'Mistral teksta notikums nav derīgs.')
                    self.text += text
                    self.deltas += 1
                    # Voxtral 3 deltas are append-only confirmed text. Persist the
                    # entire accumulated span so token spaces/punctuation survive.
                    # Session completion still requires done; a prefix is incomplete.
                    self._emit(self.text)
                elif kind == 'transcription.done':
                    # Only the authoritative done text finalizes this session span.
                    if not self.flushing or not isinstance(event.get('text'), str):
                        raise failure(ErrorCode.DISCONNECTED, 'Mistral sesija beidzās pirms audio noslēgšanas.')
                    self.text = event['text']
                    self._emit(self.text)
                    self.finished = True
                    return
                elif kind == 'error':
                    error, self.provider_error = api_error(event)
                    self.provider_error['phase'] = 'ending' if self.flushing else 'streaming'
                    raise error
                # Language/segment/turn notices are not additional transcript text.
            if not self.finished and not self.closed:
                raise failure(ErrorCode.DISCONNECTED, 'Mistral savienojums pārtrūka bez gala apstiprinājuma.')
        except Exception as error:
            self.error = safe_error(error, live=True).details
        finally:
            self.done.set()
            self.changed.set()

    async def send_audio(self, frame):
        self.check()
        if self.closed or self.flushing or self.finished:
            raise failure(ErrorCode.DISCONNECTED, 'Mistral sesija vairs nepieņem audio.')
        if (frame.segment_id != self.request.context.segment_id or frame.sample_rate != RATE
                or frame.sequence <= self.sequence or frame.source_offset_ms < self.offset):
            raise failure(ErrorCode.INVALID_AUDIO, 'Mistral audio kadra secība, segments vai frekvence nav derīga.')
        try:
            async with asyncio.timeout(self.timeout):
                self.buffer.extend(frame.pcm)
                while len(self.buffer) >= PACKET_BYTES:
                    packet = bytes(self.buffer[:PACKET_BYTES])
                    del self.buffer[:PACKET_BYTES]
                    await self._append(packet)
            self.sequence, self.offset = frame.sequence, frame.source_offset_ms
        except Exception as error:
            raise safe_error(error, live=True) from None

    async def _append(self, pcm):
        await self.raw.send(json.dumps({'type': 'input_audio.append',
            'audio': base64.b64encode(pcm).decode('ascii')}))
        self.samples += len(pcm) // 2

    async def flush(self):
        async with self.lock:
            self.check()
            if self.finished:
                return
            if self.closed:
                raise failure(ErrorCode.DISCONNECTED, 'Mistral sesija jau ir slēgta.')
            self.flushing = True
            try:
                async with asyncio.timeout(self.final_timeout):
                    if self.buffer:
                        tail = bytes(self.buffer)
                        self.buffer.clear()
                        await self._append(tail)
                    # Voxtral 3 lifecycle documents end alone. The old SDK helper
                    # also sends input_audio.flush; do not copy that legacy step.
                    await self.raw.send(json.dumps({'type': 'input_audio.end'}))
                    self.end_sent = True
                    await self.done.wait()
                self.check()
                if not self.finished:
                    raise failure(ErrorCode.DISCONNECTED, 'Mistral nav apstiprinājis audio beigas.')
            except Exception as error:
                raise safe_error(error, live=True) from None

    async def events(self):
        while True:
            if not self.queue.empty():
                yield self.queue.get_nowait()
                continue
            self.check()
            if self.closed or self.finished:
                return
            self.changed.clear()
            await self.changed.wait()

    def diagnostics(self):
        return dict(audio_samples=self.samples, text_deltas=self.deltas,
                    flush_sent=self.flush_sent, end_sent=self.end_sent,
                    done_received=self.finished, automatic_retries=0,
                    buffered_samples=len(self.buffer)//2, provider_error=self.provider_error)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.changed.set()
        self.task.cancel()
        try:
            await asyncio.gather(self.task, return_exceptions=True)
        finally:
            async with asyncio.timeout(self.timeout):
                await self.raw.close()
