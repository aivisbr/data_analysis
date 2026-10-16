"""Native Gemini file transcription through the installed google-genai SDK."""
import asyncio
import hashlib
import io
import math
import os

from app.contracts import ErrorCode, ErrorDetails, Mode, Provider
from app.providers.base import FileExecutionInfo, FileTranscript, ProviderFailure, TranscriptAnnotation
from app.providers.prepared_audio import failure, read_prepared_audio
from app.providers.settings import effective_settings

PROFILE = 'gemini:gemini-3.5-transcribe:file'


def normalize_response(response):
    if getattr(response, 'status', None) != 'completed':
        raise failure(ErrorCode.INTERNAL, 'Gemini neatgrieza pabeigtu transkripciju.')
    parts, annotations = [], []
    for step in getattr(response, 'steps', None) or []:
        if getattr(step, 'type', None) != 'model_output':
            continue
        for content in getattr(step, 'content', None) or []:
            if getattr(content, 'type', None) != 'text':
                continue
            text = getattr(content, 'text', None)
            if not isinstance(text, str):
                raise failure(ErrorCode.INTERNAL, 'Gemini transkripta teksts nav derīgs.')
            index = len(parts)
            parts.append(text)
            for annotation in getattr(content, 'annotations', None) or []:
                kind = getattr(annotation, 'type', None)
                if kind in ('speech_metadata', 'word_info'):
                    annotations.append(TranscriptAnnotation(type=kind, content_index=index,
                        **{name: getattr(annotation, name, None) for name in
                            ('text', 'speaker', 'start_index', 'end_index', 'start_offset', 'end_offset')}))
    if not parts:
        raise failure(ErrorCode.INTERNAL, 'Gemini neatgrieza transkripta teksta bloku.')
    return ''.join(parts), tuple(annotations)


def safe_error(error):
    # Files and Interactions expose different SDK exception families. Neither body is copied.
    status = getattr(error, 'status_code', None) or getattr(error, 'code', None)
    if isinstance(status, int):
        if status in (401, 403):
            return failure(ErrorCode.AUTHENTICATION, 'Gemini atslēga vai piekļuves tiesības nav derīgas.')
        if status == 404:
            return failure(ErrorCode.UNAVAILABLE_MODEL, 'Gemini modelis vai fails nav pieejams.')
        if status == 429:
            return failure(ErrorCode.RATE_LIMIT, 'Sasniegts Gemini pieprasījumu vai kvotas ierobežojums.', True)
        if status in (400, 422):
            return failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Gemini noraidīja audio vai transkripcijas iestatījumus.')
        return failure(ErrorCode.INTERNAL, 'Gemini pakalpojuma kļūda.', status >= 500)
    if isinstance(error, TimeoutError) or type(error).__name__ in ('APITimeoutError', 'ReadTimeout', 'ConnectTimeout', 'TimeoutException'):
        return failure(ErrorCode.TIMEOUT, 'Gemini transkripcijas laiks pārsniegts.', True)
    if type(error).__name__ in ('APIConnectionError', 'ConnectError', 'ClientConnectionError'):
        return failure(ErrorCode.DISCONNECTED, 'Neizdevās savienoties ar Gemini.', True)
    return failure(ErrorCode.INTERNAL, 'Gemini transkripciju nevar izpildīt.')


class GeminiFileAdapter:
    def __init__(self, *, timeout_seconds=180.0, cleanup_timeout_seconds=5.0, client_factory=None):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError('Transkripcijas taimautam jābūt no 0 līdz 600 sekundēm.')
        if not math.isfinite(cleanup_timeout_seconds) or not 0 < cleanup_timeout_seconds <= 30:
            raise ValueError('Tīrīšanas taimautam jābūt no 0 līdz 30 sekundēm.')
        self.timeout_seconds = timeout_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.client_factory = client_factory

    async def transcribe_file(self, request):
        model = request.model
        if (model.provider != Provider.GEMINI or model.mode != Mode.FILE or
                model.model_id != 'gemini-3.5-transcribe' or model.capability_profile_id != PROFILE):
            raise failure(ErrorCode.UNAVAILABLE_MODEL, 'Šis adapteris atbalsta tikai gemini-3.5-transcribe faila režīmā.')
        settings = model.effective_settings
        try:
            resolved = effective_settings(Provider.GEMINI, model.requested_settings)
        except ValueError:
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Gemini valodas iestatījums nav derīgs.') from None
        if (settings != resolved or model.capability_profile_version != 1 or settings.prompt is not None or
                settings.latency is not None or settings.turn_detection != 'default' or len(settings.keywords) > 1000):
            raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Gemini faila transkripcijas iestatījumi nav saderīgi.')
        key = os.environ.get('GEMINI_API_KEY', '').strip()
        if not key:
            raise failure(ErrorCode.AUTHENTICATION, 'Nav konfigurēta GEMINI_API_KEY atslēga.')
        try:
            from google import genai
        except ImportError:
            raise failure(ErrorCode.INTERNAL, 'google-genai Python pakotne nav pieejama.') from None
        client = None
        uploaded_name = None
        outcome = None
        primary_error = None
        cleanup_errors = []
        try:
            async with asyncio.timeout(self.timeout_seconds):
                payload, duration_ms = await asyncio.to_thread(read_prepared_audio, request)
                factory = self.client_factory or genai.Client
                client = factory(api_key=key, vertexai=False, enterprise=False,
                    http_options=dict(base_url='https://generativelanguage.googleapis.com', api_version='v1beta',
                        timeout=math.ceil(self.timeout_seconds * 1000), retry_options={'attempts': 1}))
                # Installed SDK uses distinct Files/Interactions retry semantics.
                # Files gets one attempt; explicit null configuration disables the
                # generated Interactions transport's retry engine entirely.
                interaction_api = client.aio.interactions
                interaction_api.sdk_configuration.retry_config = None
                uploaded = await client.aio.files.upload(file=io.BytesIO(payload),
                    config={'mime_type': 'audio/wav', 'display_name': 'audio.wav'})
                uploaded_name = uploaded.name
                if not uploaded_name or not uploaded.uri:
                    raise failure(ErrorCode.INTERNAL, 'Gemini neatgrieza saglabātā audio identifikatoru.')
                while getattr(uploaded.state, 'name', uploaded.state) == 'PROCESSING':
                    await asyncio.sleep(.25)
                    uploaded = await client.aio.files.get(name=uploaded_name)
                if getattr(uploaded.state, 'name', uploaded.state) != 'ACTIVE':
                    raise failure(ErrorCode.INVALID_AUDIO, 'Gemini nevar apstrādāt augšupielādēto audio.')
                config = {'mode': 'smart' if settings.formatting == 'smart' else {'type': 'verbatim'}}
                if settings.language_hint is not None:
                    config['language_codes'] = [settings.language_hint]
                if settings.keywords:
                    config['custom_vocabulary'] = list(settings.keywords)
                response = await interaction_api.create(model=model.model_id,
                    input=[{'type': 'audio', 'uri': uploaded.uri, 'mime_type': 'audio/wav'}],
                    generation_config={'transcription_config': config}, store=False, stream=False)
                text, annotations = normalize_response(response)
                outcome = FileTranscript(text=text, duration_ms=duration_ms, annotations=annotations,
                    execution=FileExecutionInfo(provider=Provider.GEMINI, model_id=model.model_id,
                        sdk_version=genai.__version__, audio_sha256=hashlib.sha256(payload).hexdigest(),
                        audio_bytes=len(payload), effective_settings=settings.evolve(
                            formatting='smart' if settings.formatting == 'smart' else 'verbatim'),
                        timeout_seconds=self.timeout_seconds, automatic_retries=0))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            primary_error = error if isinstance(error, ProviderFailure) else safe_error(error)
        finally:
            if client is not None:
                if uploaded_name:
                    try:
                        async with asyncio.timeout(self.cleanup_timeout_seconds):
                            await client.aio.files.delete(name=uploaded_name)
                    except Exception:
                        cleanup_errors.append(ErrorDetails(code=ErrorCode.INTERNAL,
                            message='Gemini pagaidu audio dzēšanu nevarēja apstiprināt.'))
                try:
                    async with asyncio.timeout(self.cleanup_timeout_seconds):
                        await client.aio.aclose()
                except Exception:
                    cleanup_errors.append(ErrorDetails(code=ErrorCode.INTERNAL,
                        message='Gemini savienojuma aizvēršanu nevarēja apstiprināt.'))
                try:
                    client.close()
                except Exception:
                    cleanup_errors.append(ErrorDetails(code=ErrorCode.INTERNAL,
                        message='Gemini sinhronā savienojuma aizvēršanu nevarēja apstiprināt.'))
        if primary_error is not None:
            raise ProviderFailure(primary_error.details, cleanup_errors=tuple(cleanup_errors)) from None
        return outcome.evolve(cleanup_errors=tuple(cleanup_errors))

    async def open_live(self, _request):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Faila adapteris neatbalsta tiešraidi.')
