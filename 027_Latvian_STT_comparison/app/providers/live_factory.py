"""Exact live routing; constructing adapters never creates network clients."""
from app.contracts import Provider, ErrorCode, ErrorDetails
from app.providers.base import ProviderFailure


def paid_live_factory(model, *, elevenlabs_enabled=False):
    if model.provider == Provider.OPENAI and model.model_id in ('gpt-transcribe', 'gpt-live-transcribe', 'gpt-realtime-whisper'):
        from app.providers.openai_live import OpenAILiveAdapter
        return OpenAILiveAdapter(allow_paid=True,final_timeout_seconds=60)
    if model.provider == Provider.GEMINI and model.model_id == 'gemini-3.5-transcribe-live':
        from app.providers.gemini_live import GeminiLiveAdapter
        return GeminiLiveAdapter(allow_paid=True,final_timeout_seconds=60)
    if model.provider == Provider.ELEVENLABS and model.model_id == 'scribe_v2_realtime':
        from app.providers.elevenlabs_live import ElevenLabsLiveAdapter
        return ElevenLabsLiveAdapter(allow_paid=elevenlabs_enabled)
    raise ProviderFailure(ErrorDetails(code=ErrorCode.UNAVAILABLE_MODEL,
                                      message='Šim modelim nav atbalstīta tiešraides adaptera.'))
