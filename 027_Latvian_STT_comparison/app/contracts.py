"""Provider-neutral validated records and events; no SDK imports or API calls."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def evolve(self, **changes):
        """Validated replacement; model_copy(update=...) skips validation."""
        return type(self).model_validate({**self.model_dump(), **changes})


class Provider(StrEnum):
    OPENAI = "openai"
    GEMINI = "gemini"
    ELEVENLABS = "elevenlabs"


class Mode(StrEnum):
    LIVE = "live"
    FILE = "file"


class ReadingLabel(StrEnum):
    A = "A"
    D = "D"


class Status(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ErrorCode(StrEnum):
    AUTHENTICATION = "authentication"
    UNAVAILABLE_MODEL = "unavailable_model"
    UNSUPPORTED_SETTINGS = "unsupported_settings"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    QUEUE_OVERFLOW = "queue_overflow"
    STORAGE = "storage"
    INVALID_AUDIO = "invalid_audio"
    INTERNAL = "internal"


class Latency(StrEnum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class TranscriptionSettings(Record):
    # None means auto detection. Provider adapters translate formats, not orchestration.
    language_hint: str | None = "lv-LV"
    keywords: tuple[str, ...] = ()
    prompt: str | None = None
    latency: Latency | None = None
    formatting: Literal["default", "verbatim", "smart"] = "default"
    turn_detection: Literal["default", "manual", "automatic"] = "default"

    @field_validator("language_hint")
    @classmethod
    def valid_language(cls, value: str | None) -> str | None:
        import re
        if value is not None and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
            raise ValueError("Valodas kods nav derīgs.")
        return value

    @field_validator("keywords")
    @classmethod
    def nonempty_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not term.strip() or "\n" in term or "\r" in term for term in values):
            raise ValueError("Atslēgvārdiem jābūt atsevišķām, netukšām frāzēm.")
        return values


class ModelEntry(Record):
    id: UUID = Field(default_factory=uuid4)
    provider: Provider
    model_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    mode: Mode
    capability_profile_id: str = Field(min_length=1)
    enabled: bool = True
    order: int = Field(default=0, ge=0)
    settings: TranscriptionSettings = Field(default_factory=TranscriptionSettings)

    @field_validator("model_id", "display_name", "capability_profile_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Vērtība nedrīkst būt tukša.")
        return value


class ModelSnapshot(Record):
    entry_id: UUID
    provider: Provider
    model_id: str
    display_name: str
    mode: Mode
    capability_profile_id: str
    capability_profile_version: int = Field(default=1, ge=1)
    requested_settings: TranscriptionSettings
    effective_settings: TranscriptionSettings

    @classmethod
    def from_entry(cls, entry: ModelEntry, *, effective: TranscriptionSettings | None = None):
        if not entry.enabled:
            raise ValueError("Izslēgtu modeli nevar iekļaut izpildē.")
        return cls(entry_id=entry.id, provider=entry.provider, model_id=entry.model_id,
                   display_name=entry.display_name, mode=entry.mode,
                   capability_profile_id=entry.capability_profile_id,
                   requested_settings=entry.settings,
                   effective_settings=effective if effective is not None else entry.settings)


class CaptureSegment(Record):
    id: UUID = Field(default_factory=uuid4)
    index: int = Field(ge=0)
    # Milliseconds since run start versus accepted audio only (paused audio excluded).
    source_start_ms: int = Field(ge=0)
    source_end_ms: int | None = Field(default=None, ge=0)
    audio_start_ms: int = Field(ge=0)
    audio_end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_times(self):
        if (self.source_end_ms is None) != (self.audio_end_ms is None):
            raise ValueError("Segmenta beigu laikiem jābūt norādītiem kopā.")
        if self.source_end_ms is not None:
            if self.source_end_ms < self.source_start_ms or self.audio_end_ms < self.audio_start_ms:
                raise ValueError("Segmenta beigas nevar būt pirms sākuma.")
        return self


class ErrorDetails(Record):
    code: ErrorCode
    # Safe, localized text supplied by the adapter; never raw exceptions or credentials.
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class AttemptRecord(Record):
    id: UUID = Field(default_factory=uuid4)
    model_entry_id: UUID
    number: int = Field(default=1, ge=1)
    segment_ids: tuple[UUID, ...] = ()
    status: Status = Status.QUEUED
    errors: tuple[ErrorDetails, ...] = ()
    raw_file: str | None = None
    editable_file: str | None = None
    save_status: Literal["pending", "saved", "failed"] = "pending"

    @model_validator(mode="after")
    def valid_outcome(self):
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("Segmentu identifikatori nedrīkst atkārtoties.")
        if self.status == Status.COMPLETE and self.errors:
            raise ValueError("Izpildi ar neatrisinātām kļūdām nevar atzīmēt kā pabeigtu.")
        if self.status in (Status.INCOMPLETE, Status.FAILED, Status.INTERRUPTED) and not self.errors:
            raise ValueError("Nepilnīgai izpildei jānorāda iemesls.")
        if self.save_status == "saved" and (not self.raw_file or not self.editable_file):
            raise ValueError("Saglabātai izpildei jānorāda abi teksta faili.")
        return self


class ReadingRecord(Record):
    label: ReadingLabel
    status: Status = Status.QUEUED
    segments: tuple[CaptureSegment, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()

    @model_validator(mode="after")
    def valid_segments(self):
        if [segment.index for segment in self.segments] != list(range(len(self.segments))):
            raise ValueError("Segmentiem jābūt secīgiem, sākot ar nulli.")
        ids = {segment.id for segment in self.segments}
        if len(ids) != len(self.segments):
            raise ValueError("Segmentu identifikatoriem jābūt unikāliem.")
        for previous, following in zip(self.segments, self.segments[1:]):
            if previous.source_end_ms is None or previous.audio_end_ms is None:
                raise ValueError("Pirms nākamā segmenta jāaizver iepriekšējais.")
            if following.source_start_ms < previous.source_end_ms or following.audio_start_ms != previous.audio_end_ms:
                raise ValueError("Segmentu laiki pārklājas vai pieņemtajā audio ir atstarpe.")
        attempt_numbers = {(attempt.model_entry_id, attempt.number) for attempt in self.attempts}
        if len(attempt_numbers) != len(self.attempts):
            raise ValueError("Modeļa mēģinājuma numurs nedrīkst atkārtoties lasījumā.")
        if any(not set(attempt.segment_ids).issubset(ids) for attempt in self.attempts):
            raise ValueError("Mēģinājums norāda cita lasījuma segmentu.")
        indices = {segment.id: segment.index for segment in self.segments}
        for attempt in self.attempts:
            order = [indices[id_] for id_ in attempt.segment_ids]
            if order != sorted(order):
                raise ValueError("Mēģinājuma segmentiem jābūt lasījuma secībā.")
        latest = {}
        for attempt in self.attempts:
            if attempt.model_entry_id not in latest or attempt.number > latest[attempt.model_entry_id].number:
                latest[attempt.model_entry_id] = attempt
        if self.status == Status.COMPLETE and (not latest or any(
            attempt.status != Status.COMPLETE for attempt in latest.values()
        )):
            raise ValueError("Lasījums ar nepabeigtu mēģinājumu nav pilnībā pabeigts.")
        return self


class RunRecord(Record):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utc_now)
    mode: Mode
    status: Status = Status.QUEUED
    models: tuple[ModelSnapshot, ...]
    readings: tuple[ReadingRecord, ...] = ()
    schema_version: Literal[1] = 1

    @field_validator("created_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Laikam jāietver laika josla.")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_links(self):
        model_ids = {model.entry_id for model in self.models}
        if not model_ids or len(model_ids) != len(self.models):
            raise ValueError("Izvēlieties vismaz vienu modeli ar unikālu identifikatoru.")
        if any(model.mode != self.mode for model in self.models):
            raise ValueError("Modeļa un izpildes režīmi nesakrīt.")
        labels = [reading.label for reading in self.readings]
        if len(set(labels)) != len(labels):
            raise ValueError("A un D lasījumam katram jābūt atsevišķam ierakstam.")
        attempts = [attempt for reading in self.readings for attempt in reading.attempts]
        segment_ids = [segment.id for reading in self.readings for segment in reading.segments]
        if len(set(segment_ids)) != len(segment_ids) or len({attempt.id for attempt in attempts}) != len(attempts):
            raise ValueError("Segmentu un mēģinājumu identifikatoriem izpildē jābūt unikāliem.")
        if any(attempt.model_entry_id not in model_ids for attempt in attempts):
            raise ValueError("Mēģinājuma modelis nav izvēlēts šai izpildei.")
        if self.status == Status.COMPLETE and (not self.readings or any(
            reading.status != Status.COMPLETE for reading in self.readings
        )):
            raise ValueError("Izpilde ar nepabeigtu lasījumu nav pilnībā pabeigta.")
        if self.status == Status.COMPLETE and any(
            {attempt.model_entry_id for attempt in reading.attempts} != model_ids
            for reading in self.readings
        ):
            raise ValueError("Pabeigtam lasījumam nepieciešami visu izvēlēto modeļu rezultāti.")
        return self


class EventContext(Record):
    run_id: UUID
    reading: ReadingLabel
    attempt_id: UUID
    model_entry_id: UUID
    segment_id: UUID
    segment_index: int = Field(ge=0)

    def validate_for_run(self, run: RunRecord) -> None:
        if self.run_id != run.id:
            raise ValueError("Notikums pieder citai izpildei.")
        reading = next((item for item in run.readings if item.label == self.reading), None)
        if reading is None:
            raise ValueError("Notikuma lasījums nav atrasts.")
        attempt = next((item for item in reading.attempts if item.id == self.attempt_id), None)
        if attempt is None or attempt.model_entry_id != self.model_entry_id:
            raise ValueError("Notikuma mēģinājums vai modelis neatbilst lasījumam.")
        segment = next((item for item in reading.segments if item.id == self.segment_id), None)
        if segment is None or segment.index != self.segment_index or segment.id not in attempt.segment_ids:
            raise ValueError("Notikuma segments neatbilst mēģinājumam.")


class EventBase(Record):
    event_id: UUID = Field(default_factory=uuid4)
    context: EventContext


class TranscriptEvent(EventBase):
    # Adapter assigns chronological order from input turns, not completion arrival.
    span_id: str = Field(min_length=1)
    span_index: int = Field(ge=0)
    revision: int = Field(ge=1)
    text: str  # Full replacement text, including empty text to erase a provisional span.


class PartialTranscriptEvent(TranscriptEvent):
    type: Literal["transcript.partial"] = "transcript.partial"


class FinalTranscriptEvent(TranscriptEvent):
    type: Literal["transcript.final"] = "transcript.final"


class ErrorEvent(EventBase):
    type: Literal["attempt.error"] = "attempt.error"
    error: ErrorDetails
    fatal: bool = True


class StatusEvent(EventBase):
    type: Literal["attempt.status"] = "attempt.status"
    status: Status


ProviderEvent = Annotated[
    PartialTranscriptEvent | FinalTranscriptEvent | ErrorEvent | StatusEvent,
    Field(discriminator="type"),
]
