"""Scribe v2 native file transcription; no SDK imports or clients at startup."""
import asyncio
import hashlib
from importlib.metadata import version
import math
import os

from app.contracts import ErrorCode, Mode, Provider
from app.providers.base import FileExecutionInfo, FileTranscript, ProviderFailure, TranscriptAnnotation
from app.providers.prepared_audio import failure, read_prepared_audio
from app.providers.settings import effective_settings

PROFILE = 'elevenlabs:scribe_v2:file'


def normalize_response(response):
    text = getattr(response, 'text', None)
    words = getattr(response, 'words', None)
    if not isinstance(text, str) or not isinstance(words, list):
        raise failure(ErrorCode.INTERNAL, 'ElevenLabs neatgrieza derīgu transkriptu.')
    annotations, spoken, all_tokens, has_events = [], [], [], False
    for word in words:
        kind, token = getattr(word, 'type', None), getattr(word, 'text', None)
        if kind not in ('word', 'spacing', 'audio_event') or not isinstance(token, str):
            raise failure(ErrorCode.INTERNAL, 'ElevenLabs transkripta vārdu dati nav derīgi.')
        all_tokens.append(token)
        if kind == 'audio_event':
            has_events = True
        else:
            spoken.append(token)
        if kind == 'spacing':
            if token.strip():
                raise failure(ErrorCode.INTERNAL, 'ElevenLabs atstarpju dati nav derīgi.')
            continue
        times = []
        for name in ('start', 'end'):
            value = getattr(word, name, None)
            if value is not None and (not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0):
                raise failure(ErrorCode.INTERNAL, 'ElevenLabs laika atzīmes nav derīgas.')
            times.append(value)
        if times[0] is not None and times[1] is not None and times[1] < times[0]:
            raise failure(ErrorCode.INTERNAL, 'ElevenLabs laika atzīmju secība nav derīga.')
        annotations.append(TranscriptAnnotation(type='audio_event' if kind == 'audio_event' else 'word_info',
            content_index=0, text=token, speaker=getattr(word, 'speaker_id', None),
            start_offset=None if times[0] is None else f'{times[0]}s',
            end_offset=None if times[1] is None else f'{times[1]}s'))
    # Normally events are disabled and exact response text is retained. Unexpected
    # typed events are removed using word/spacing tokens, never regex guesses.
    if has_events and ''.join(all_tokens) != text:
        raise failure(ErrorCode.INTERNAL, 'ElevenLabs audio notikumu tekstu nevar droši atdalīt.')
    return ''.join(spoken) if has_events else text, tuple(annotations)


class ElevenLabsFileAdapter:
    def __init__(self, *, timeout_seconds=180.0, client_factory=None, http_client_factory=None):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError('Transkripcijas taimautam jābūt no 0 līdz 600 sekundēm.')
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory
        self.http_client_factory = http_client_factory

    async def transcribe_file(self, request):
        model = request.model
        if (model.provider != Provider.ELEVENLABS or model.mode != Mode.FILE or
                model.model_id != 'scribe_v2' or model.capability_profile_id != PROFILE):
            raise failure(ErrorCode.UNAVAILABLE_MODEL, 'Šis adapteris atbalsta tikai scribe_v2 faila režīmā.')
        settings = model.effective_settings
        try:
            resolved = effective_settings(Provider.ELEVENLABS, model.requested_settings)
        except ValueError:
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'ElevenLabs valodas iestatījums nav derīgs.') from None
        if (settings != resolved or model.capability_profile_version != 1 or settings.prompt is not None or
                settings.latency is not None or settings.turn_detection != 'default' or len(settings.keywords) > 1000 or
                any(len(term) >= 50 or len(term.split()) > 5 or any(c in term for c in '<>{}[]\\') for term in settings.keywords)):
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'ElevenLabs faila transkripcijas iestatījumi nav saderīgi.')
        key = os.environ.get('ELEVENLABS_API_KEY', '').strip()
        if not key:
            raise failure(ErrorCode.AUTHENTICATION, 'Nav konfigurēta ELEVENLABS_API_KEY atslēga.')
        try:
            import httpx
            from elevenlabs.client import AsyncElevenLabs
            from elevenlabs.core.api_error import ApiError
        except ImportError:
            raise failure(ErrorCode.INTERNAL, 'ElevenLabs Python pakotne nav pieejama.') from None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                payload, duration_ms = await asyncio.to_thread(read_prepared_audio, request, min_seconds=.1)
                http_factory = self.http_client_factory or httpx.AsyncClient
                async with http_factory(timeout=self.timeout_seconds, follow_redirects=False) as transport:
                    factory = self.client_factory or AsyncElevenLabs
                    client = factory(api_key=key, base_url='https://api.elevenlabs.io', timeout=self.timeout_seconds,
                        follow_redirects=False, httpx_client=transport)
                    parameters = dict(model_id=model.model_id, file=('audio.wav', payload, 'audio/wav'),
                        tag_audio_events=False, diarize=False, timestamps_granularity='word', file_format='other',
                        webhook=False, use_multi_channel=False, no_verbatim=settings.formatting == 'smart',
                        request_options={'timeout_in_seconds': self.timeout_seconds, 'max_retries': 0})
                    if settings.language_hint is not None:
                        parameters['language_code'] = settings.language_hint
                    if settings.keywords:
                        parameters['keyterms'] = list(settings.keywords)
                    response = await client.speech_to_text.convert(**parameters)
                text, annotations = normalize_response(response)
                language = getattr(response, 'language_code', None)
                if language is not None and not isinstance(language, str):
                    raise failure(ErrorCode.INTERNAL, 'ElevenLabs valodas atbilde nav derīga.')
                return FileTranscript(text=text, annotations=annotations, duration_ms=duration_ms,
                    detected_language=language or None, detected_languages=(language,) if language else (),
                    execution=FileExecutionInfo(provider=Provider.ELEVENLABS, model_id=model.model_id,
                        sdk_version=version('elevenlabs'), audio_sha256=hashlib.sha256(payload).hexdigest(),
                        audio_bytes=len(payload), effective_settings=settings.evolve(
                            formatting='smart' if settings.formatting == 'smart' else 'verbatim'),
                        timeout_seconds=self.timeout_seconds, automatic_retries=0))
        except ProviderFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise failure(ErrorCode.TIMEOUT, 'ElevenLabs transkripcijas laiks pārsniegts.', True) from None
        except httpx.RequestError:
            raise failure(ErrorCode.DISCONNECTED, 'Neizdevās savienoties ar ElevenLabs.', True) from None
        except ApiError as error:
            status = error.status_code
            if status in (401, 403):
                code, message, retryable = ErrorCode.AUTHENTICATION, 'ElevenLabs atslēga vai piekļuves tiesības nav derīgas.', False
            elif status == 404:
                code, message, retryable = ErrorCode.UNAVAILABLE_MODEL, 'ElevenLabs modelis vai galapunkts nav pieejams.', False
            elif status == 429:
                code, message, retryable = ErrorCode.RATE_LIMIT, 'Sasniegts ElevenLabs pieprasījumu vai kvotas ierobežojums.', True
            elif status in (400, 422):
                code, message, retryable = ErrorCode.UNSUPPORTED_SETTINGS, 'ElevenLabs noraidīja audio vai transkripcijas iestatījumus.', False
            else:
                code, message, retryable = ErrorCode.INTERNAL, 'ElevenLabs pakalpojuma kļūda.', isinstance(status, int) and status >= 500
            raise failure(code, message, retryable) from None
        except Exception:
            raise failure(ErrorCode.INTERNAL, 'ElevenLabs transkripciju nevar izpildīt.') from None

    async def open_live(self, _request):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Faila adapteris neatbalsta tiešraidi.')
