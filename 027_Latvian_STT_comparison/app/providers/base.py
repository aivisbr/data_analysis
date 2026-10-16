"""Structural adapter interfaces with provider-neutral input/output types."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts import ErrorDetails, EventContext, Mode, ModelSnapshot, Provider, ProviderEvent, Record, TranscriptionSettings


@dataclass(frozen=True)
class AudioFrame:
    segment_id: UUID
    sequence: int
    source_offset_ms: int
    sample_rate: int
    pcm: bytes  # Signed little-endian 16-bit mono PCM; binary, not JSON text.

    def __post_init__(self):
        if self.sequence < 0 or self.source_offset_ms < 0 or self.sample_rate <= 0:
            raise ValueError("Audio kadra laiks, secība vai frekvence nav derīga.")
        if not isinstance(self.pcm, bytes) or not self.pcm or len(self.pcm) % 2:
            raise ValueError("Audio kadram jābūt netukšiem 16 bitu PCM paraugiem.")


class ProviderRequest(Record):
    context: EventContext
    model: ModelSnapshot

    def model_post_init(self, _context):
        if self.context.model_entry_id != self.model.entry_id:
            raise ValueError("Pieprasījuma modelis neatbilst notikuma kontekstam.")


class FileRequest(ProviderRequest):
    audio_path: Path
    mime_type: str = Field(min_length=1)
    expected_audio_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Workers supply the preparation hash to verify the bytes actually submitted.

    @model_validator(mode="after")
    def file_mode(self):
        if self.model.mode != Mode.FILE:
            raise ValueError("Faila pieprasījumam nepieciešams faila režīma modelis.")
        return self


class LiveRequest(ProviderRequest):
    @model_validator(mode="after")
    def live_mode(self):
        if self.model.mode != Mode.LIVE:
            raise ValueError("Tiešraidei nepieciešams tiešraides modelis.")
        return self


class FileExecutionInfo(Record):
    provider: Provider
    model_id: str
    sdk_version: str
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audio_bytes: int = Field(gt=0)
    effective_settings: TranscriptionSettings
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    automatic_retries: int = Field(ge=0)


class TranscriptAnnotation(Record):
    type: Literal['word_info', 'speech_metadata', 'audio_event']
    content_index: int = Field(ge=0)
    text: str | None = None
    speaker: str | None = None
    start_index: int | None = Field(default=None, ge=0)
    end_index: int | None = Field(default=None, ge=0)
    start_offset: str | None = None
    end_offset: str | None = None


class FileTranscript(Record):
    text: str
    detected_language: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    detected_languages: tuple[str, ...] = ()
    execution: FileExecutionInfo | None = None
    annotations: tuple[TranscriptAnnotation, ...] = ()
    cleanup_errors: tuple[ErrorDetails, ...] = ()


class ProviderFailure(Exception):
    """Safe failure; adapters must never attach SDK exceptions or response bodies."""

    def __init__(self, details: ErrorDetails, *, cleanup_errors: tuple[ErrorDetails, ...] = ()):
        self.details = details
        self.cleanup_errors = cleanup_errors
        super().__init__(details.message)


@runtime_checkable
class LiveSession(Protocol):
    async def send_audio(self, frame: AudioFrame) -> None: ...

    def events(self) -> AsyncIterator[ProviderEvent]: ...

    async def flush(self) -> None:
        """Commit accepted audio; keep receiving events until terminal or timeout."""
        ...

    async def close(self) -> None:
        """Idempotent cleanup; orchestration supplies independent time bounds."""
        ...


@runtime_checkable
class ProviderAdapter(Protocol):
    async def transcribe_file(self, request: FileRequest) -> FileTranscript: ...

    async def open_live(self, request: LiveRequest) -> LiveSession: ...
