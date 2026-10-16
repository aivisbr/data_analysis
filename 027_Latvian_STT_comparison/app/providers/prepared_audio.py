"""SDK-free prepared WAV validation shared by file adapters."""
import hashlib
import io
import wave

from app.audio import MAX_CHUNK_BYTES, MAX_CHUNK_SECONDS, SAMPLE_RATE
from app.contracts import ErrorCode, ErrorDetails
from app.providers.base import ProviderFailure


def failure(code, message, retryable=False):
    return ProviderFailure(ErrorDetails(code=code, message=message, retryable=retryable))


def read_prepared_audio(request, *, max_bytes=MAX_CHUNK_BYTES, max_seconds=MAX_CHUNK_SECONDS, min_seconds=0):
    if request.mime_type != 'audio/wav' or request.audio_path.suffix.lower() != '.wav':
        raise failure(ErrorCode.INVALID_AUDIO, 'Nepieciešams sagatavots WAV audio fragments.')
    try:
        with request.audio_path.open('rb') as source:
            payload = source.read(max_bytes + 1)
        if not payload or len(payload) > max_bytes:
            raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragments ir tukšs vai pārsniedz izmēra limitu.')
        if request.expected_audio_sha256 is not None and hashlib.sha256(payload).hexdigest() != request.expected_audio_sha256:
            raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragmenta kontrolsumma neatbilst sagatavei.')
        with wave.open(io.BytesIO(payload), 'rb') as wav:
            frames = wav.getnframes()
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
                raise failure(ErrorCode.INVALID_AUDIO, 'Nepieciešams mono 24 kHz 16 bitu PCM WAV.')
            if not 0 < frames <= max_seconds * SAMPLE_RATE or len(wav.readframes(frames)) != frames * 2:
                raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragments ir bojāts vai pārsniedz ilguma limitu.')
            if frames < min_seconds * SAMPLE_RATE:
                raise failure(ErrorCode.INVALID_AUDIO, 'Audio fragments ir pārāk īss šim galapunktam.')
        return payload, round(frames * 1000 / SAMPLE_RATE)
    except (OSError, EOFError, wave.Error):
        raise failure(ErrorCode.INVALID_AUDIO, 'Sagatavoto audio failu nevar nolasīt.') from None
