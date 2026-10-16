"""Local, bounded audio preparation. No provider calls; chunk intervals never overlap."""

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import wave
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.contracts import Record, ReadingLabel

MAX_UPLOAD_BYTES = 250_000_000
MAX_DURATION_SECONDS = 7200
SAMPLE_RATE = 24000
# Conservative common transport policy, not a claim about every endpoint's limit.
MAX_CHUNK_BYTES = 24_000_000
MAX_CHUNK_SECONDS = 300
EXTENSIONS = {'.wav', '.mp3', '.m4a', '.mp4', '.webm', '.ogg', '.flac', '.aac'}
ENDPOINT_LIMITS = {
    'openai': dict(max_bytes=25_000_000, max_seconds=None,
        source='https://developers.openai.com/api/docs/guides/speech-to-text'),
    'gemini': dict(max_bytes=None, max_seconds=1800,
        source='https://ai.google.dev/gemini-api/docs/transcribe'),
    'elevenlabs': dict(max_bytes=3_000_000_000, max_seconds=3600,
        source='https://elevenlabs.io/docs/overview/capabilities/speech-to-text/'),
}


def ensure_directory(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AudioError() from None


class AudioError(Exception):
    def __init__(self, message="Audio failu nevar saglabāt vai nolasīt.", code="storage"):
        self.code = code
        super().__init__(message)


class PreparationRequest(Record):
    audio_id: UUID
    reading: ReadingLabel
    start_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    end_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    chunk_seconds: int = Field(default=300, ge=1, le=MAX_CHUNK_SECONDS)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("Intervāla beigām jābūt pēc sākuma.")
        return self


def digest(path):
    try:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError:
        raise AudioError() from None


def command(args, timeout=180):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except FileNotFoundError:
        raise AudioError("Nav atrasts FFmpeg vai ffprobe. Pievieno tos PATH un pārstartē lietotni.", "unavailable") from None
    except subprocess.TimeoutExpired:
        raise AudioError("Audio apstrādes laiks pārsniegts.", "invalid") from None
    if result.returncode:
        # Codec diagnostics may contain paths; never echo subprocess input/output.
        raise AudioError("Audio formātu nevar dekodēt vai fails ir bojāts.", "invalid")
    return result.stdout


class AudioStore:
    def __init__(self, root: Path):
        self.root = root
        ensure_directory(self.root)

    def _load(self, kind, identity):
        directory = self.root / kind / str(UUID(str(identity)))
        try:
            metadata = json.loads((directory / 'metadata.json').read_text('utf-8'))
            if not isinstance(metadata, dict) or metadata.get('id') != str(identity) or metadata.get('schema_version') != 1:
                raise AudioError()
            return directory, metadata
        except FileNotFoundError:
            raise AudioError("Audio ieraksts nav atrasts.", "not_found") from None
        except (OSError, ValueError):
            raise AudioError() from None

    @staticmethod
    def _publish(stage, target, metadata):
        for path in stage.iterdir():
            # Windows FlushFileBuffers requires a writable handle.
            with path.open('rb+') as stream:
                os.fsync(stream.fileno())
        with (stage / 'metadata.json').open('x', encoding='utf-8') as output:
            json.dump(metadata, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.rename(stage, target)

    def upload(self, filename, stream):
        name = filename.replace('\\', '/').split('/')[-1]
        suffix = Path(name).suffix.lower()
        if suffix not in EXTENSIONS:
            raise AudioError("Izvēlies WAV, MP3, M4A, MP4, WebM, OGG, FLAC vai AAC failu.", "invalid")
        identity = uuid4()
        parent = self.root / 'uploads'
        ensure_directory(parent)
        stage = parent / ('.' + str(identity))
        ensure_directory(stage)
        try:
            original = stage / ('original' + suffix)
            size = 0
            with original.open('xb') as output:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_UPLOAD_BYTES:
                        raise AudioError("Fails pārsniedz lietotnes 250 MB ierobežojumu.", "invalid")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            probe = json.loads(command(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-show_streams', '-show_format', '-of', 'json', str(original)], 30))
            audio = next((s for s in probe.get('streams', []) if s.get('codec_type') == 'audio'), None)
            duration = float(probe.get('format', {}).get('duration', 0))
            if not audio or not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
                raise AudioError("Nepieciešams audio celiņš ar zināmu ilgumu līdz 2 stundām.", "invalid")
            # Browser-safe preview also verifies full decoding, with hard output/time bounds.
            self._convert(original, stage / 'preview.wav', 0, duration)
            with wave.open(str(stage / 'preview.wav'), 'rb') as preview:
                decoded_duration = preview.getnframes() / SAMPLE_RATE
            if not 0 < decoded_duration <= MAX_DURATION_SECONDS:
                raise AudioError("Dekodētais audio ir tukšs vai pārsniedz 2 stundas.", "invalid")
            data = dict(schema_version=1, id=str(identity), filename=name, original_file=original.name,
                sha256=digest(original), bytes=size, duration_seconds=duration,
                decoded_duration_seconds=decoded_duration,
                audio_stream_index=audio['index'], codec=audio.get('codec_name'),
                sample_rate=audio.get('sample_rate'), channels=audio.get('channels'),
                preview_file='preview.wav', preview_sha256=digest(stage / 'preview.wav'))
            self._publish(stage, parent / str(identity), data)
            return data
        except AudioError:
            raise
        except (OSError, ValueError, KeyError, wave.Error):
            raise AudioError() from None
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    @staticmethod
    def _convert(source, target, start, duration):
        command(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe',
            '-i', str(source), '-ss', str(start), '-t', str(duration), '-map', '0:a:0',
            '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le',
            '-fs', str(MAX_DURATION_SECONDS * SAMPLE_RATE * 2 + 10000), '-n', str(target)])

    def list_uploads(self):
        parent = self.root / 'uploads'
        if not parent.exists():
            return []
        items = []
        for child in parent.iterdir():
            if child.name.startswith('.'):
                continue
            try:
                items.append(self._load('uploads', UUID(child.name))[1])
            except (ValueError, AudioError):
                continue
        return items

    def upload_path(self, identity, kind):
        folder, metadata = self._load('uploads', identity)
        field = 'original_file' if kind == 'original' else 'preview_file'
        path = folder / metadata[field]
        if path.parent != folder or digest(path) != metadata['sha256' if kind == 'original' else 'preview_sha256']:
            raise AudioError("Saglabātā audio kontrolsumma neatbilst.")
        return path

    def prepare(self, request: PreparationRequest):
        _, source = self._load('uploads', request.audio_id)
        end = request.end_seconds if request.end_seconds is not None else source['duration_seconds']
        if not request.start_seconds < end <= source['duration_seconds']:
            raise AudioError("Intervāls atrodas ārpus audio faila.", "invalid")
        original = self.upload_path(request.audio_id, 'original')
        identity = uuid4()
        parent = self.root / 'prepared'
        ensure_directory(parent)
        stage = parent / ('.' + str(identity))
        ensure_directory(stage)
        try:
            interval = stage / 'interval.wav'
            self._convert(original, interval, request.start_seconds, end - request.start_seconds)
            chunks = []
            with wave.open(str(interval), 'rb') as source_wav:
                total = source_wav.getnframes()
                if not total:
                    raise AudioError("Izvēlētais intervāls nesatur audio paraugus.", "invalid")
                frames_per_chunk = min(request.chunk_seconds * SAMPLE_RATE, (MAX_CHUNK_BYTES - 44) // 2)
                offset = 0
                while offset < total:
                    count = min(frames_per_chunk, total - offset)
                    filename = f'chunk_{len(chunks):04d}.wav'
                    path = stage / filename
                    with wave.open(str(path), 'wb') as target:
                        target.setparams(source_wav.getparams())
                        target.writeframes(source_wav.readframes(count))
                    if path.stat().st_size > MAX_CHUNK_BYTES:
                        raise AudioError("Sagatavotais fragments pārsniedz faila limitu.", "invalid")
                    for limit in ENDPOINT_LIMITS.values():
                        if ((limit['max_bytes'] is not None and path.stat().st_size > limit['max_bytes']) or
                                (limit['max_seconds'] is not None and count / SAMPLE_RATE > limit['max_seconds'])):
                            raise AudioError("Fragments pārsniedz nodrošinātāja audio limitu.", "invalid")
                    chunks.append(dict(index=len(chunks), file=filename, start_sample=offset,
                        end_sample=offset + count, source_start_seconds=request.start_seconds + offset / SAMPLE_RATE,
                        source_end_seconds=request.start_seconds + (offset + count) / SAMPLE_RATE,
                        sha256=digest(path), bytes=path.stat().st_size))
                    offset += count
            data = dict(schema_version=1, id=str(identity), audio_id=str(request.audio_id),
                reading=request.reading, directory='Try1' if request.reading == ReadingLabel.A else 'Try2',
                original_sha256=source['sha256'], request=request.model_dump(mode='json'),
                source_interval=dict(start_seconds=request.start_seconds, end_seconds=end),
                conversion=dict(codec='pcm_s16le', sample_rate=SAMPLE_RATE, channels=1,
                    ffmpeg_version=command(['ffmpeg', '-version'], 10).decode().splitlines()[0],
                    sample_count=total, actual_duration_seconds=total / SAMPLE_RATE,
                    interval_sha256=digest(interval)),
                chunk_policy=dict(max_bytes=MAX_CHUNK_BYTES, max_seconds=request.chunk_seconds,
                    overlap_samples=0, merge='concatenate in index order; never text-deduplicate'),
                endpoint_limits=dict(checked_date='2026-10-14', limits=ENDPOINT_LIMITS,
                    scope='initial file profiles; conservative feature-inclusive duration limits; custom IDs need adapter validation'),
                chunks=chunks)
            self._publish(stage, parent / str(identity), data)
            return data
        except AudioError:
            raise
        except (OSError, ValueError, wave.Error):
            raise AudioError() from None
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def prepared_path(self, identity, chunk_index=None):
        folder, metadata = self._load('prepared', identity)
        if chunk_index is None:
            name, expected = 'interval.wav', metadata['conversion']['interval_sha256']
        else:
            if not 0 <= chunk_index < len(metadata['chunks']):
                raise AudioError("Audio fragments nav atrasts.", "not_found")
            chunk = metadata['chunks'][chunk_index]
            name, expected = chunk['file'], chunk['sha256']
        path = folder / name
        if path.parent != folder or digest(path) != expected:
            raise AudioError("Sagatavotā audio kontrolsumma neatbilst.")
        return path
