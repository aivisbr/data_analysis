"""Durable transcript checkpoints and immutable exports; no provider calls.

One store owns writes in the single-process local app. Call from worker threads,
not directly from the async audio loop. Final publication needs hard-link support
(Windows NTFS); unsupported storage fails explicitly rather than overwriting files.
"""

from hashlib import sha256
import os
from pathlib import Path
import tempfile
import time
from threading import RLock
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter, ValidationError, model_validator

from app.contracts import (
    AttemptRecord, ErrorCode, ErrorDetails, ErrorEvent, FinalTranscriptEvent,
    PartialTranscriptEvent, ProviderEvent, ReadingLabel, Record, RunRecord, Status,
    TranscriptEvent,
)


READING_DIRECTORIES = {ReadingLabel.A: "Try1", ReadingLabel.D: "Try2"}
TERMINAL = {Status.COMPLETE, Status.INCOMPLETE, Status.FAILED,
            Status.INTERRUPTED, Status.CANCELLED}
STORAGE_ERROR = ErrorDetails(code=ErrorCode.STORAGE,
                             message="Transkriptu nevar saglabāt diskā.", retryable=True)
EVENT_ADAPTER = TypeAdapter(ProviderEvent)


class TranscriptStorageError(Exception):
    def __init__(self):
        self.error = STORAGE_ERROR
        super().__init__(self.error.message)


class AttemptCheckpoint(Record):
    attempt_id: UUID
    spans: tuple[PartialTranscriptEvent | FinalTranscriptEvent, ...] = ()
    seen_event_ids: tuple[UUID, ...] = ()
    events: tuple[ErrorEvent, ...] = ()
    sealed: bool = False
    raw_sha256: str | None = None
    save_error: ErrorDetails | None = None


class TranscriptCheckpoint(Record):
    schema_version: Literal[1] = 1
    run: RunRecord
    attempts: tuple[AttemptCheckpoint, ...] = ()

    @model_validator(mode="after")
    def valid_checkpoint(self):
        attempts = {a.id: a for r in self.run.readings for a in r.attempts}
        states = {s.attempt_id: s for s in self.attempts}
        if len(states) != len(self.attempts) or not states.keys() <= attempts.keys():
            raise ValueError("Kontrolpunkta mēģinājumu identitāte nav derīga.")
        for state in self.attempts:
            if len(set(state.seen_event_ids)) != len(state.seen_event_ids):
                raise ValueError("Kontrolpunkta notikumu identitāte nav derīga.")
            span_keys, positions = set(), set()
            for event in (*state.spans, *state.events):
                event.context.validate_for_run(self.run)
                if event.context.attempt_id != state.attempt_id:
                    raise ValueError("Kontrolpunkta notikums pieder citam mēģinājumam.")
                if isinstance(event, TranscriptEvent):
                    key = (event.context.segment_id, event.span_id)
                    position = (event.context.segment_id, event.span_index)
                    if key in span_keys or position in positions:
                        raise ValueError("Kontrolpunkta fragmenti atkārtojas.")
                    span_keys.add(key)
                    positions.add(position)
            if state.sealed and (attempts[state.attempt_id].status not in TERMINAL or
                    state.raw_sha256 != sha256(finalized_text(state).encode("utf-8")).hexdigest()):
                raise ValueError("Noslēgtā transkripta kontrolpunkts nav derīgs.")
        if any(a.save_status == "saved" and (a.id not in states or not states[a.id].sealed)
               for a in attempts.values()):
            raise ValueError("Saglabātam transkriptam jābūt noslēgtam kontrolpunktam.")
        return self


def finalized_text(state: AttemptCheckpoint) -> str:
    """Join whole finalized spans chronologically, without provisional text."""
    ordered = sorted(state.spans, key=lambda event: (event.context.segment_index, event.span_index))
    return " ".join(event.text.strip() for event in ordered
                    if isinstance(event, FinalTranscriptEvent) and event.text.strip())


def _write_atomic(path: Path, payload: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Atomic publish-if-absent. Never os.replace a raw or editable export.
            os.link(temporary, path)
        else:
            # Windows readers (history downloads, virus scanners) may briefly
            # hold a handle without FILE_SHARE_DELETE. Retry this local publish,
            # never the provider request or transcript event.
            for attempt in range(21):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as error:
                    if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 20:
                        raise
                    time.sleep(.01)
    finally:
        Path(temporary).unlink(missing_ok=True)


class TranscriptStore:
    def __init__(self, runs_dir: Path):
        self.root = runs_dir.resolve()
        self._lock = RLock()

    def _path(self, run_id: UUID, *parts: str) -> Path:
        path = self.root.joinpath(str(UUID(str(run_id))), *parts)
        # UUID-generated names only, and reject symlinks/junctions outside storage.
        if not path.resolve().is_relative_to(self.root):
            raise TranscriptStorageError()
        return path

    def exists(self, run_id: UUID) -> bool:
        return self._path(run_id, "metadata.json").is_file()

    def load(self, run_id: UUID) -> TranscriptCheckpoint:
        with self._lock:
            try:
                checkpoint = TranscriptCheckpoint.model_validate_json(
                    self._path(run_id, "metadata.json").read_bytes())
                if checkpoint.run.id != run_id:
                    raise ValueError("Mismatched run")
                return checkpoint
            except (OSError, ValueError, ValidationError):
                raise TranscriptStorageError() from None

    def _save(self, checkpoint: TranscriptCheckpoint) -> None:
        try:
            _write_atomic(self._path(checkpoint.run.id, "metadata.json"),
                          checkpoint.model_dump_json(indent=2).encode("utf-8"))
        except OSError:
            raise TranscriptStorageError() from None

    @staticmethod
    def _attempt(run: RunRecord, attempt_id: UUID):
        for reading in run.readings:
            for attempt in reading.attempts:
                if attempt.id == attempt_id:
                    return reading, attempt
        raise ValueError("Mēģinājums nav atrasts.")

    @staticmethod
    def _replace_attempt(run: RunRecord, updated: AttemptRecord) -> RunRecord:
        return run.evolve(readings=tuple(reading.evolve(attempts=tuple(
            updated if item.id == updated.id else item for item in reading.attempts
        )) for reading in run.readings))

    @staticmethod
    def _replace_state(checkpoint, state, *, run=None):
        states = tuple(item for item in checkpoint.attempts if item.attempt_id != state.attempt_id)
        return checkpoint.evolve(run=run or checkpoint.run, attempts=(*states, state))

    def checkpoint_run(self, run: RunRecord) -> None:
        """Persist orchestration progress without changing selection or old exports."""
        with self._lock:
            if self.exists(run.id):
                old = self.load(run.id)
                if (run.models, run.mode, run.created_at) != (
                    old.run.models, old.run.mode, old.run.created_at
                ):
                    raise ValueError("Izpildes sākotnējo izvēli nevar mainīt.")
                for reading in old.run.readings:
                    for attempt in reading.attempts:
                        new_reading, new = self._attempt(run, attempt.id)
                        if (new.save_status, new.raw_file, new.editable_file) != (
                            attempt.save_status, attempt.raw_file, attempt.editable_file
                        ):
                            raise ValueError("Saglabāšanas statusu nosaka tikai failu saglabāšana.")
                        if new_reading.label != reading.label or (
                            new.model_entry_id, new.number, new.segment_ids[:len(attempt.segment_ids)]
                        ) != (attempt.model_entry_id, attempt.number, attempt.segment_ids):
                            raise ValueError("Mēģinājuma identitāti vai iepriekšējos segmentus nevar mainīt.")
                        state = next((s for s in old.attempts if s.attempt_id == attempt.id), None)
                        if state and state.sealed and new != attempt:
                            raise ValueError("Noslēgtu mēģinājumu nevar mainīt.")
                # Existing segment indexes/timestamps must not reinterpret recorded events.
                for reading in old.run.readings:
                    newer = next((r for r in run.readings if r.label == reading.label), None)
                    if newer is None:
                        raise ValueError("Iepriekšējo lasījumu nevar dzēst.")
                    for before in reading.segments:
                        after = next((s for s in newer.segments if s.id == before.id), None)
                        if after is None:
                            raise ValueError("Iepriekšējo segmentu nevar dzēst.")
                        if before.source_end_ms is None:
                            after = after.evolve(source_end_ms=None, audio_end_ms=None)
                        if after != before:
                            raise ValueError("Iepriekšējo segmentu nevar pārrakstīt.")
                old_ids = {a.id for r in old.run.readings for a in r.attempts}
                if any(a.id not in old_ids and (a.save_status != "pending" or a.raw_file or a.editable_file)
                       for r in run.readings for a in r.attempts):
                    raise ValueError("Jaunu mēģinājumu nevar atzīmēt kā jau saglabātu.")
                checkpoint = old.evolve(run=run)
            else:
                if any(a.save_status != "pending" or a.raw_file or a.editable_file
                       for r in run.readings for a in r.attempts):
                    raise ValueError("Jaunu izpildi nevar atzīmēt kā jau saglabātu.")
                checkpoint = TranscriptCheckpoint(run=run)
            self._save(checkpoint)

    def accept(self, event: ProviderEvent) -> bool:
        """Checkpoint an event before returning; false means duplicate/stale/late."""
        event = EVENT_ADAPTER.validate_python(event)
        with self._lock:
            checkpoint = self.load(event.context.run_id)
            event.context.validate_for_run(checkpoint.run)
            state = next((s for s in checkpoint.attempts if s.attempt_id == event.context.attempt_id),
                         AttemptCheckpoint(attempt_id=event.context.attempt_id))
            if state.sealed or event.event_id in state.seen_event_ids:
                return False
            accepted = True
            if isinstance(event, TranscriptEvent):
                previous = next((s for s in state.spans if
                    (s.context.segment_id, s.span_id) == (event.context.segment_id, event.span_id)), None)
                if previous and previous.span_index != event.span_index:
                    raise ValueError("Teksta fragmenta secību nevar mainīt.")
                if any(s.context.segment_id == event.context.segment_id and
                       s.span_index == event.span_index and s.span_id != event.span_id for s in state.spans):
                    raise ValueError("Fragmenta secības numurs jau izmantots.")
                accepted = not previous or (
                    event.revision > previous.revision and not (
                        isinstance(previous, FinalTranscriptEvent) and isinstance(event, PartialTranscriptEvent)))
                if accepted:
                    spans = tuple(s for s in state.spans if s != previous)
                    state = state.evolve(spans=(*spans, event))
            elif isinstance(event, ErrorEvent):
                state = state.evolve(events=(*state.events, event))
            # Status events are advisory; only orchestration updates processing status.
            state = state.evolve(seen_event_ids=(*state.seen_event_ids, event.event_id))
            self._save(self._replace_state(checkpoint, state))
            return accepted

    def finalize(self, run_id: UUID, attempt_id: UUID, *, status: Status,
                 errors: tuple[ErrorDetails, ...] = ()) -> AttemptRecord:
        """Seal results, then publish raw + initial editable copy, preserving retries."""
        if status not in TERMINAL:
            raise ValueError("Mēģinājumam jābūt noslēgtam.")
        with self._lock:
            checkpoint = self.load(run_id)
            reading, attempt = self._attempt(checkpoint.run, attempt_id)
            state = next((s for s in checkpoint.attempts if s.attempt_id == attempt_id),
                         AttemptCheckpoint(attempt_id=attempt_id))
            payload = finalized_text(state).encode("utf-8")
            digest = sha256(payload).hexdigest()
            if state.sealed:
                if (attempt.status, attempt.errors) != (status, errors) or state.raw_sha256 != digest:
                    raise ValueError("Noslēgta mēģinājuma rezultātu nevar mainīt.")
            else:
                attempt = attempt.evolve(status=status, errors=errors)
                state = state.evolve(sealed=True, raw_sha256=digest)
                checkpoint = self._replace_state(checkpoint, state,
                    run=self._replace_attempt(checkpoint.run, attempt))
                # Persist the sealed content before any final file can appear.
                self._save(checkpoint)
            name = f"{attempt.model_entry_id}_{attempt.number}_{attempt.id}.txt"
            folder = READING_DIRECTORIES[reading.label]
            raw = self._path(run_id, folder, "raw", name)
            editable = self._path(run_id, folder, "editable", name)
            try:
                if raw.exists():
                    if sha256(raw.read_bytes()).hexdigest() != digest:
                        raise OSError("Raw export changed")
                else:
                    _write_atomic(raw, payload, exclusive=True)
                if not editable.exists():
                    _write_atomic(editable, payload, exclusive=True)
                # Confirm both outputs exist and can be read, including an edited copy.
                editable.read_bytes()
                attempt = attempt.evolve(save_status="saved",
                    raw_file=raw.relative_to(self.root / str(run_id)).as_posix(),
                    editable_file=editable.relative_to(self.root / str(run_id)).as_posix())
                state = state.evolve(save_error=None)
            except OSError:
                attempt = attempt.evolve(save_status="failed")
                state = state.evolve(save_error=STORAGE_ERROR)
            checkpoint = self._replace_state(checkpoint, state,
                run=self._replace_attempt(checkpoint.run, attempt))
            self._save(checkpoint)
            return attempt

    def export_path(self, run_id: UUID, attempt_id: UUID,
                    kind: Literal["raw", "editable"]) -> Path:
        """Resolve only registered exports, never an arbitrary request path."""
        if kind not in ("raw", "editable"):
            raise ValueError("Faila veids nav derīgs.")
        with self._lock:
            checkpoint = self.load(run_id)
            reading, attempt = self._attempt(checkpoint.run, attempt_id)
            state = next((s for s in checkpoint.attempts if s.attempt_id == attempt_id), None)
            if attempt.save_status != "saved" or state is None or not state.sealed:
                raise TranscriptStorageError()
            name = f"{attempt.model_entry_id}_{attempt.number}_{attempt.id}.txt"
            path = self._path(run_id, READING_DIRECTORIES[reading.label], kind, name)
            try:
                content = path.read_bytes()
                if kind == "raw" and sha256(content).hexdigest() != state.raw_sha256:
                    raise ValueError("Raw export changed")
            except (OSError, ValueError):
                raise TranscriptStorageError() from None
            return path
