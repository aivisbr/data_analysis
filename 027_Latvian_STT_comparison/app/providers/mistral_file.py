"""Exact Voxtral 3 file models, native async SDK, one request per prepared chunk."""
import asyncio
import hashlib
import math
import os
from importlib.metadata import version

from app.contracts import ErrorCode, Mode, Provider
from app.providers.base import FileExecutionInfo, FileTranscript
from app.providers.mistral_common import failure, safe_error, validate
from app.providers.prepared_audio import read_prepared_audio


class MistralFileAdapter:
    def __init__(self, *, timeout_seconds=180, client_factory=None):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError('Invalid timeout')
        self.timeout, self.factory = timeout_seconds, client_factory

    async def transcribe_file(self, request):
        settings = validate(request, Mode.FILE)
        key = os.environ.get('MISTRAL_API_KEY', '').strip()
        if not key:
            raise failure(ErrorCode.AUTHENTICATION, 'Nav konfigurēta MISTRAL_API_KEY atslēga.')
        try:
            from mistralai.client import Mistral
        except ImportError:
            raise failure(ErrorCode.INTERNAL, 'Mistral Python pakotne nav pieejama.') from None
        try:
            async with asyncio.timeout(self.timeout):
                payload, duration = await asyncio.to_thread(read_prepared_audio, request)
                parameters = dict(model=request.model.model_id, file={'file_name': 'audio.wav', 'content': payload},
                                  diarize=False, retries=None, timeout_ms=round(self.timeout * 1000))
                if settings.language_hint:
                    parameters['language'] = settings.language_hint
                if settings.keywords:
                    parameters['context_bias'] = list(settings.keywords)
                # Close both SDK-owned sync and async clients. No automatic retries or redirects to custom hosts.
                with (self.factory or Mistral)(api_key=key, server_url='https://api.mistral.ai',
                        retry_config=None, timeout_ms=round(self.timeout * 1000)) as client:
                    async with client:
                        result = await client.audio.transcriptions.complete_async(**parameters)
                if not isinstance(result.text, str):
                    raise failure(ErrorCode.INTERNAL, 'Mistral neatgrieza derīgu transkriptu.')
                language = getattr(result, 'language', None)
                return FileTranscript(text=result.text, detected_language=language if isinstance(language, str) else None,
                    duration_ms=duration, execution=FileExecutionInfo(provider=Provider.MISTRAL,
                        model_id=request.model.model_id, sdk_version=version('mistralai'),
                        audio_sha256=hashlib.sha256(payload).hexdigest(), audio_bytes=len(payload),
                        effective_settings=settings, timeout_seconds=self.timeout, automatic_retries=0))
        except Exception as error:
            raise safe_error(error) from None
