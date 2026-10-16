"""Single prepared WAV → OpenAI final text. SDK imports and clients are lazy."""
import asyncio
import hashlib
import io
import math
import os
import wave

from app.audio import MAX_CHUNK_BYTES, MAX_CHUNK_SECONDS, SAMPLE_RATE
from app.contracts import ErrorCode, ErrorDetails, Mode, Provider
from app.providers.base import FileExecutionInfo, FileTranscript, ProviderFailure
from app.providers.settings import effective_settings

PROFILE = 'openai:gpt-transcribe:file'


def failure(code, message, retryable=False):
    return ProviderFailure(ErrorDetails(code=code, message=message, retryable=retryable))


def read_prepared_audio(request):
    if request.mime_type != 'audio/wav' or request.audio_path.suffix.lower() != '.wav':
        raise failure(ErrorCode.INVALID_AUDIO, 'Nepieciešams sagatavots WAV audio fragments.')
    try:
        with request.audio_path.open('rb') as source:
            payload = source.read(MAX_CHUNK_BYTES + 1)
        if not payload or len(payload) > MAX_CHUNK_BYTES:
            raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragments ir tukšs vai pārsniedz 24 MB.')
        if request.expected_audio_sha256 is not None and hashlib.sha256(payload).hexdigest() != request.expected_audio_sha256:
            raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragmenta kontrolsumma neatbilst sagatavei.')
        with wave.open(io.BytesIO(payload), 'rb') as wav:
            frames = wav.getnframes()
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
                raise failure(ErrorCode.INVALID_AUDIO, 'Nepieciešams mono 24 kHz 16 bitu PCM WAV.')
            if not 0 < frames <= MAX_CHUNK_SECONDS * SAMPLE_RATE or len(wav.readframes(frames)) != frames * 2:
                raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragments ir bojāts vai pārsniedz 5 minūtes.')
        return payload, round(frames * 1000 / SAMPLE_RATE)
    except (OSError, EOFError, wave.Error):
        raise failure(ErrorCode.INVALID_AUDIO, 'Sagatavoto audio failu nevar nolasīt.') from None


class OpenAIFileAdapter:
    def __init__(self, *, timeout_seconds=180.0, client_factory=None):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError('Transkripcijas taimautam jābūt no 0 līdz 600 sekundēm.')
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory

    async def transcribe_file(self, request):
        model = request.model
        # Explicit ID guard: no deprecated models, fallback or unverified custom calls.
        if (model.provider != Provider.OPENAI or model.mode != Mode.FILE or
                model.model_id != 'gpt-transcribe' or model.capability_profile_id != PROFILE):
            raise failure(ErrorCode.UNAVAILABLE_MODEL, 'Šis adapteris atbalsta tikai gpt-transcribe faila režīmā.')
        settings = model.effective_settings
        try:
            resolved = effective_settings(Provider.OPENAI, model.requested_settings)
        except ValueError:
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'OpenAI valodas iestatījums nav derīgs.') from None
        if (model.capability_profile_version != 1 or settings != resolved or settings.latency is not None or
                settings.formatting != 'default' or settings.turn_detection != 'default' or
                any(any(c in term for c in '<>\r\n') for term in settings.keywords)):
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'OpenAI faila transkripcijas iestatījumi nav saderīgi.')
        # Read only at execution, never at startup or module import; do not display it.
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not key:
            raise failure(ErrorCode.AUTHENTICATION, 'Nav konfigurēta OPENAI_API_KEY atslēga.')
        try:
            import openai
        except ImportError:
            raise failure(ErrorCode.INTERNAL, 'OpenAI Python pakotne nav pieejama.') from None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                payload, duration_ms = await asyncio.to_thread(read_prepared_audio, request)
                parameters = dict(model=model.model_id, file=('audio.wav', payload, 'audio/wav'),
                    response_format='json', stream=False)
                if settings.language_hint is not None:
                    parameters['languages'] = [settings.language_hint]
                if settings.keywords:
                    parameters['keywords'] = list(settings.keywords)
                if settings.prompt is not None:
                    parameters['prompt'] = settings.prompt
                factory = self.client_factory or openai.AsyncOpenAI
                # Pin the destination; OPENAI_BASE_URL must not redirect credentials.
                async with factory(api_key=key, base_url='https://api.openai.com/v1',
                        timeout=self.timeout_seconds, max_retries=0) as client:
                    response = await client.audio.transcriptions.create(**parameters)
                text = getattr(response, 'text', None)
                if not isinstance(text, str):
                    raise failure(ErrorCode.INTERNAL, 'OpenAI neatgrieza derīgu transkripta tekstu.')
                languages = getattr(response, 'languages', None) or []
                codes = []
                for item in languages:
                    code = item.get('code') if isinstance(item, dict) else getattr(item, 'code', None)
                    if isinstance(code, str) and code and code not in codes:
                        codes.append(code)
                return FileTranscript(text=text, detected_languages=tuple(codes),
                    detected_language=codes[0] if len(codes) == 1 else None, duration_ms=duration_ms,
                    execution=FileExecutionInfo(provider=Provider.OPENAI, model_id=model.model_id,
                        sdk_version=openai.__version__, audio_sha256=hashlib.sha256(payload).hexdigest(),
                        audio_bytes=len(payload), effective_settings=settings,
                        timeout_seconds=self.timeout_seconds, automatic_retries=0))
        except ProviderFailure:
            raise
        except (TimeoutError, openai.APITimeoutError):
            raise failure(ErrorCode.TIMEOUT, 'OpenAI transkripcijas laiks pārsniegts.', True) from None
        except (openai.AuthenticationError, openai.PermissionDeniedError):
            raise failure(ErrorCode.AUTHENTICATION, 'OpenAI atslēga vai piekļuves tiesības nav derīgas.') from None
        except openai.NotFoundError:
            raise failure(ErrorCode.UNAVAILABLE_MODEL, 'OpenAI modelis vai galapunkts nav pieejams.') from None
        except openai.RateLimitError:
            raise failure(ErrorCode.RATE_LIMIT, 'Sasniegts OpenAI pieprasījumu vai kvotas ierobežojums.', True) from None
        except (openai.BadRequestError, openai.UnprocessableEntityError):
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'OpenAI noraidīja audio vai transkripcijas iestatījumus.') from None
        except openai.APIConnectionError:
            raise failure(ErrorCode.DISCONNECTED, 'Neizdevās savienoties ar OpenAI.', True) from None
        except openai.APIStatusError as error:
            raise failure(ErrorCode.INTERNAL, 'OpenAI pakalpojuma kļūda.', error.status_code >= 500) from None
        except Exception:
            raise failure(ErrorCode.INTERNAL, 'OpenAI transkripciju nevar izpildīt.') from None

    async def open_live(self, _request):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Faila adapteris neatbalsta tiešraidi.')
