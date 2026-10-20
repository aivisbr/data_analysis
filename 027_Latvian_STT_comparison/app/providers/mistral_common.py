"""Voxtral 3 validation and safe errors, with no SDK import or network at startup."""
from app.contracts import ErrorCode, ErrorDetails, Mode, Provider
from app.providers.base import ProviderFailure
from app.providers.settings import effective_settings

LIVE_MODEL = 'voxtral-mini-transcribe-realtime-3-0'
FILE_MODELS = ('voxtral-small-transcribe-3-0', 'voxtral-mini-transcribe-3-0')
DELAYS = {'minimal': 80, 'low': 240, 'medium': 480, 'high': 1200, 'xhigh': 2400}


def failure(code, message):
    return ProviderFailure(ErrorDetails(code=code, message=message))


def validate(request, mode):
    model = request.model
    allowed = FILE_MODELS if mode == Mode.FILE else (LIVE_MODEL,)
    if (model.provider != Provider.MISTRAL or model.mode != mode or model.model_id not in allowed
            or model.capability_profile_id != f'mistral:{model.model_id}:{mode.value}'):
        raise failure(ErrorCode.UNAVAILABLE_MODEL, 'Šim Mistral modelim nav atbilstoša Voxtral 3 adaptera.')
    settings = model.effective_settings
    try:
        resolved = effective_settings(Provider.MISTRAL, model.requested_settings)
    except ValueError:
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Mistral valodas iestatījums nav derīgs.') from None
    if (model.capability_profile_version != 1 or settings != resolved or settings.prompt is not None
            or settings.formatting != 'default' or settings.turn_detection not in ('default', 'manual')
            or (mode == Mode.FILE and (settings.latency is not None or settings.turn_detection != 'default'))):
        raise failure(ErrorCode.UNSUPPORTED_SETTINGS, 'Mistral adapteris neatbalsta izvēlētos iestatījumus.')
    return settings


def safe_error(error, *, live=False):
    if isinstance(error, ProviderFailure):
        return error
    if isinstance(error, TimeoutError):
        return failure(ErrorCode.TIMEOUT, 'Mistral atbildes gaidīšanas laiks beidzās; pieprasījums netiks atkārtots.')
    status = getattr(error, 'status_code', None) or getattr(getattr(error, 'response', None), 'status_code', None)
    code = {401: ErrorCode.AUTHENTICATION, 403: ErrorCode.AUTHENTICATION,
            404: ErrorCode.UNAVAILABLE_MODEL, 429: ErrorCode.RATE_LIMIT,
            400: ErrorCode.UNSUPPORTED_SETTINGS, 422: ErrorCode.UNSUPPORTED_SETTINGS}.get(
                status, ErrorCode.DISCONNECTED if live else ErrorCode.INTERNAL)
    return failure(code, 'Mistral pieprasījums neizdevās. Pārbaudi atslēgu, modeļa piekļuvi un iestatījumus.')
