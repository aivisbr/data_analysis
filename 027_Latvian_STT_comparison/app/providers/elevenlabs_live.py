"""Native ElevenLabs realtime SDK; explicit, serialized commits and no reconnect."""
import asyncio
import base64
import math
import os

from app.contracts import (Provider, Mode, ErrorCode, ErrorDetails,
                           PartialTranscriptEvent, FinalTranscriptEvent, ErrorEvent)
from app.providers.base import ProviderFailure
from app.providers.settings import effective_settings

MODEL = 'scribe_v2_realtime'
RATE = 24000


def failure(code, message):
    return ProviderFailure(ErrorDetails(code=code, message=message))


def live_config(request):
    model = request.model
    if (model.provider != Provider.ELEVENLABS or model.mode != Mode.LIVE or
            model.model_id != MODEL or model.capability_profile_id != f'elevenlabs:{MODEL}:live'):
        raise failure(ErrorCode.UNAVAILABLE_MODEL, 'Šis adapteris atbalsta tikai scribe_v2_realtime.')
    settings = model.effective_settings
    try:
        resolved = effective_settings(Provider.ELEVENLABS, model.requested_settings)
    except ValueError:
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'ElevenLabs valodas iestatījums nav derīgs.') from None
    if (settings != resolved or model.capability_profile_version != 1 or
            settings.prompt is not None or settings.latency is not None or
            settings.turn_detection not in ('default', 'manual') or len(settings.keywords) > 50 or
            any(len(term) > 20 for term in settings.keywords)):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS,
                      'ElevenLabs tiešraide atbalsta manuālus apstiprinājumus un līdz 50 terminiem, katru līdz 20 rakstzīmēm; uzvedne un latentuma iestatījumi nav atbalstīti.')
    config = dict(model_id=MODEL, audio_format='pcm_24000', sample_rate=RATE,
                  commit_strategy='manual', include_timestamps=False,
                  include_language_detection=False, no_verbatim=settings.formatting == 'smart')
    if settings.language_hint:
        config['language_code'] = settings.language_hint
    if settings.keywords:
        config['keyterms'] = list(settings.keywords)
    return config


def safe_error(error):
    if isinstance(error, ProviderFailure):
        return error
    if isinstance(error, TimeoutError):
        return failure(ErrorCode.TIMEOUT, 'ElevenLabs tiešraides atbildes gaidīšanas laiks beidzās.')
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    code = {401: ErrorCode.AUTHENTICATION, 403: ErrorCode.AUTHENTICATION,
            429: ErrorCode.RATE_LIMIT, 400: ErrorCode.UNSUPPORTED_SETTINGS}.get(status, ErrorCode.DISCONNECTED)
    return failure(code, 'ElevenLabs tiešraides savienojuma kļūda.')


class ElevenLabsLiveAdapter:
    def __init__(self, *, timeout_seconds=10, client_factory=None, allow_paid=False):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError('Invalid timeout')
        self.timeout = timeout_seconds
        self.factory = client_factory
        self.allow_paid = allow_paid

    async def open_live(self, request):
        config = live_config(request)
        if not self.allow_paid:
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Maksas ElevenLabs tiešraides izpilde nav iespējota.')
        key = os.environ.get('ELEVENLABS_API_KEY', '').strip()
        if not key:
            raise failure(ErrorCode.AUTHENTICATION, 'Nav konfigurēta ELEVENLABS_API_KEY atslēga.')
        try:
            from elevenlabs.realtime.scribe import ScribeRealtime, AudioFormat, CommitStrategy
        except ImportError:
            raise failure(ErrorCode.INTERNAL, 'ElevenLabs Python pakotne nav pieejama.') from None
        session = None
        try:
            client = (self.factory or ScribeRealtime)(api_key=key, base_url='wss://api.elevenlabs.io')
            options = {**config, 'audio_format': AudioFormat.PCM_24000,
                       'commit_strategy': CommitStrategy.MANUAL}
            async with asyncio.timeout(self.timeout):
                raw = await client.connect(options)
            # connect() schedules its receive task but does not yield after creating it.
            # Register all callbacks before the next await, including session_started.
            session = ElevenLabsLiveSession(request, config, raw, self.timeout)
            async with asyncio.timeout(self.timeout):
                await session.ready.wait()
            session.check_error()
            return session
        except BaseException as error:
            if session:
                try:
                    await session.close()
                except Exception:
                    pass
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise safe_error(error) from None


class ElevenLabsLiveSession:
    def __init__(self, request, config, raw, timeout):
        self.request, self.config, self.raw, self.timeout = request, config, raw, timeout
        self.queue = asyncio.Queue(256)
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.closed = self.finished = self.flushing = self.pending = False
        self.error = None
        self.sequence = self.offset = -1
        self.buffer = bytearray()
        self.uncommitted_samples = 0
        self.index = self.revision = 0
        self.partial = self.last_final = None
        raw.on('session_started', self._started)
        raw.on('partial_transcript', lambda data: self._transcript(data, False))
        raw.on('committed_transcript', lambda data: self._transcript(data, True))
        # Delayed timestamp/language/edited variants are deliberately not subscribed.
        raw.on('error', self._error)
        raw.on('close', self._disconnected)

    def check_error(self):
        if self.error:
            raise ProviderFailure(self.error)

    def _fail(self, code, message):
        if self.closed or self.error:
            return
        self.error = ErrorDetails(code=code, message=message)
        if not self.queue.full():
            self.queue.put_nowait(ErrorEvent(context=self.request.context, error=self.error))
        self.ready.set()
        self.changed.set()

    def _started(self, data):
        config = data.get('config', {}) if isinstance(data, dict) else {}
        required = ('model_id', 'audio_format', 'sample_rate')
        if any(config.get(key) != self.config[key] for key in required):
            self._fail(ErrorCode.UNSUPPORTED_SETTINGS, 'ElevenLabs neapstiprināja pieprasīto audio formātu vai modeli.')
        elif any(key in config and config[key] != value for key, value in self.config.items()):
            self._fail(ErrorCode.UNSUPPORTED_SETTINGS, 'ElevenLabs apstiprinātie iestatījumi neatbilst pieprasījumam.')
        else:
            self.ready.set()

    def _error(self, data):
        kind = data.get('message_type') if isinstance(data, dict) else None
        codes = {'auth_error': ErrorCode.AUTHENTICATION, 'unaccepted_terms': ErrorCode.AUTHENTICATION,
                 'quota_exceeded': ErrorCode.RATE_LIMIT, 'rate_limited': ErrorCode.RATE_LIMIT,
                 'commit_throttled': ErrorCode.RATE_LIMIT, 'resource_exhausted': ErrorCode.RATE_LIMIT,
                 'queue_overflow': ErrorCode.QUEUE_OVERFLOW, 'session_time_limit_exceeded': ErrorCode.TIMEOUT,
                 'input_error': ErrorCode.INVALID_AUDIO, 'chunk_size_exceeded': ErrorCode.INVALID_AUDIO,
                 'insufficient_audio_activity': ErrorCode.INVALID_AUDIO,
                 'invalid_request': ErrorCode.UNSUPPORTED_SETTINGS}
        self._fail(codes.get(kind, ErrorCode.DISCONNECTED), 'ElevenLabs tiešraides apstrādes kļūda.')

    def _disconnected(self, _data=None):
        if not self.finished:
            self._fail(ErrorCode.DISCONNECTED, 'ElevenLabs audio savienojums pārtrūka.')

    def _transcript(self, data, final):
        if self.closed or self.error or self.finished:
            return
        text = data.get('text') if isinstance(data, dict) else None
        if not isinstance(text, str) or len(text) > 1024 * 1024:
            self._fail(ErrorCode.INTERNAL, 'ElevenLabs transkripta dati nav derīgi.')
            return
        if final and not self.pending:
            # Only one explicit commit is outstanding. Never deduplicate by text
            # across different requested commits: repeated speech is legitimate.
            if text == self.last_final:
                return
            self._fail(ErrorCode.INTERNAL, 'ElevenLabs saņemts negaidīts apstiprinājums.')
            return
        if not final and (not self.uncommitted_samples or text == self.partial):
            return
        if self.index >= 10000 or self.queue.full():
            self._fail(ErrorCode.QUEUE_OVERFLOW, 'ElevenLabs transkriptu rinda ir pārpildīta.')
            return
        self.revision += 1
        event = FinalTranscriptEvent if final else PartialTranscriptEvent
        self.queue.put_nowait(event(context=self.request.context, span_id=f'utterance_{self.index}',
                                   span_index=self.index, revision=self.revision, text=text))
        self.changed.set()
        if final:
            self.last_final = text
            self.index += 1
            self.revision = 0
            self.partial = None
            self.pending = False
            self.uncommitted_samples = 0
            self.changed.set()
        else:
            self.partial = text

    async def _operation(self, operation):
        try:
            async with asyncio.timeout(self.timeout):
                await operation()
        except Exception as error:
            raise safe_error(error) from None

    async def _send(self, pcm):
        await self._operation(lambda: self.raw.send({'audio_base_64': base64.b64encode(pcm).decode('ascii')}))

    async def _commit(self):
        self.check_error()
        if not self.uncommitted_samples:
            return
        self.pending = True
        await self._operation(self.raw.commit)
        try:
            async with asyncio.timeout(self.timeout):
                while self.pending:
                    self.changed.clear()
                    self.check_error()
                    await self.changed.wait()
                self.check_error()
        except Exception as error:
            raise safe_error(error) from None

    async def send_audio(self, frame):
        async with self.lock:
            self.check_error()
            if (self.closed or self.flushing or frame.segment_id != self.request.context.segment_id or
                    frame.sample_rate != RATE or frame.sequence <= self.sequence or frame.source_offset_ms < self.offset or
                    len(frame.pcm) > RATE * 2):
                raise failure(ErrorCode.INVALID_AUDIO, 'ElevenLabs audio kadra secība vai formāts nav derīgs.')
            self.sequence, self.offset = frame.sequence, frame.source_offset_ms
            self.buffer.extend(frame.pcm)
            # Recommended 100 ms packets; no padding or dropped tail samples.
            while len(self.buffer) >= 4800:
                pcm = bytes(self.buffer[:4800])
                del self.buffer[:4800]
                self.uncommitted_samples += len(pcm) // 2
                await self._send(pcm)
                if self.uncommitted_samples >= RATE * 20:
                    await self._commit()

    async def flush(self):
        async with self.lock:
            self.check_error()
            if self.closed or self.finished:
                return
            self.flushing = True
            if self.pending:
                raise failure(ErrorCode.DISCONNECTED, 'Iepriekšējais ElevenLabs apstiprinājums nav pabeigts.')
            if self.buffer:
                pcm = bytes(self.buffer)
                self.buffer.clear()
                self.uncommitted_samples += len(pcm) // 2
                await self._send(pcm)
            await self._commit()
            self.finished = True
            self.changed.set()

    async def events(self):
        while True:
            if not self.queue.empty():
                yield self.queue.get_nowait()
                continue
            if self.error:
                raise ProviderFailure(self.error)
            if self.closed or self.finished:
                return
            self.changed.clear()
            # Callbacks run on this same event loop; no yield occurs between
            # checking the queue and clearing the notification.
            await self.changed.wait()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.changed.set()
        try:
            await self._operation(self.raw.close)
        finally:
            # SDK close can fail before cancelling its owned message handler.
            task = getattr(self.raw, '_message_task', None)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
