"""Local text selection and immutable evaluation input snapshots. No scoring yet."""

from datetime import datetime
from hashlib import sha256
from importlib.metadata import version
import os
from pathlib import Path
import platform
import shutil
import tempfile
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.contracts import ModelSnapshot, ReadingLabel, Record, Status, utc_now
from app.transcripts import TranscriptStore, _write_atomic


MAX_TEXT_BYTES = 2 * 1024 * 1024


class EvaluationInputError(Exception):
    def __init__(self, message="Vērtēšanas failus nevar saglabāt vai nolasīt.", *, code="storage"):
        self.code = code
        super().__init__(message)


def decode_text(payload: bytes, *, ground_truth=False) -> str:
    if len(payload) > MAX_TEXT_BYTES:
        raise EvaluationInputError("Teksta fails pārsniedz 2 MiB.", code="invalid")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise EvaluationInputError("Teksta failam jābūt UTF-8 kodējumā.", code="invalid") from None
    if "\x00" in text:
        raise EvaluationInputError("Fails satur nederīgas teksta rakstzīmes.", code="invalid")
    if ground_truth and not text.strip():
        raise EvaluationInputError("Etalona teksts nedrīkst būt tukšs.", code="invalid")
    return text


class UploadedSelection(Record):
    kind: Literal["upload"] = "upload"
    input_id: UUID


class ManagedSelection(Record):
    kind: Literal["managed"] = "managed"
    run_id: UUID
    attempt_id: UUID
    variant: Literal["raw", "edited"]


HypothesisSelection = Annotated[UploadedSelection | ManagedSelection, Field(discriminator="kind")]


class UploadedInput(Record):
    id: UUID = Field(default_factory=uuid4)
    role: Literal["ground_truth", "hypothesis"]
    filename: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    reading: ReadingLabel | None = None
    variant: Literal["raw", "edited"] | None = None
    uploaded_at: datetime = Field(default_factory=utc_now)
    original_sha256: str
    original_size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_role(self):
        if self.role == "hypothesis" and (self.reading is None or self.variant is None):
            raise ValueError("Transkriptam jānorāda lasījums un faila veids.")
        if self.role == "ground_truth" and (self.reading is not None or self.variant is not None):
            raise ValueError("Etalons nav transkripta lasījums vai labojums.")
        if not self.label.strip():
            raise ValueError("Faila nosaukums nedrīkst būt tukšs.")
        return self


class ScoringConfiguration(Record):
    # Named, versioned policies for T008/T009. T007 computes no metrics.
    schema_version: Literal[1] = 1
    legacy_normalization: Literal["evaluate-cli-v1"] = "evaluate-cli-v1"
    preserve_diacritics: Literal[True] = True
    strict_normalization: Literal["nfc-whitespace-v1"] = "nfc-whitespace-v1"
    punctuation_profile: Literal["exact-v1"] = "exact-v1"


class EvaluationRequest(Record):
    ground_truth: UploadedSelection
    hypotheses: tuple[HypothesisSelection, ...] = Field(min_length=1, max_length=100)
    configuration: ScoringConfiguration = Field(default_factory=ScoringConfiguration)

    @model_validator(mode="after")
    def no_duplicates(self):
        if len(set(self.hypotheses)) != len(self.hypotheses):
            raise ValueError("Transkripts izvēlēts vairākas reizes.")
        return self


class InputSnapshot(Record):
    source: UploadedSelection | ManagedSelection
    role: Literal["ground_truth", "hypothesis"]
    label: str
    reading: ReadingLabel | None
    variant: Literal["raw", "edited"] | None
    file: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=MAX_TEXT_BYTES)
    model: ModelSnapshot | None = None
    processing_status: Status | None = None


class EvaluationSnapshot(Record):
    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utc_now)
    status: Literal["inputs_saved"] = "inputs_saved"
    reading: ReadingLabel
    variant: Literal["raw", "edited"]
    configuration: ScoringConfiguration
    python_version: str
    unicode_version: str
    jiwer_version: str
    scorer_version: Literal["not-run"] = "not-run"
    ground_truth: InputSnapshot
    hypotheses: tuple[InputSnapshot, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_inputs(self):
        if (self.ground_truth.role != "ground_truth" or
                not isinstance(self.ground_truth.source, UploadedSelection) or
                self.ground_truth.reading is not None or self.ground_truth.variant is not None):
            raise ValueError("Etalona momentuzņēmums nav derīgs.")
        if any(item.role != "hypothesis" or item.reading != self.reading or
               item.variant != self.variant for item in self.hypotheses):
            raise ValueError("Momentuzņēmumā sajaukti lasījumi vai failu veidi.")
        if len({item.source for item in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("Momentuzņēmumā transkripti atkārtojas.")
        return self


class EvaluationInputStore:
    def __init__(self, inputs_dir: Path, evaluations_dir: Path, transcripts: TranscriptStore):
        self.inputs_root = inputs_dir.resolve()
        self.root = evaluations_dir.resolve()
        self.transcripts = transcripts

    @staticmethod
    def _path(root: Path, identifier: UUID, *parts: str) -> Path:
        path = root.joinpath(str(UUID(str(identifier))), *parts)
        if not path.resolve().is_relative_to(root):
            raise EvaluationInputError()
        return path

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            with path.open("rb") as stream:
                payload = stream.read(MAX_TEXT_BYTES + 1)
        except OSError:
            raise EvaluationInputError() from None
        decode_text(payload)
        return payload

    @staticmethod
    def _publish(root: Path, identifier: UUID, files: dict[str, bytes]):
        """Publish the complete directory; metadata never exposes partial snapshots."""
        stage = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            destination = EvaluationInputStore._path(root, identifier)
            stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
            for name, payload in files.items():
                _write_atomic(stage / name, payload, exclusive=True)
            os.rename(stage, destination)
        except OSError:
            raise EvaluationInputError() from None
        finally:
            if stage is not None and stage.exists():
                # Delete only our generated staging directory inside the verified root.
                if stage.resolve().parent == root and stage.name.startswith(".pending-"):
                    try:
                        shutil.rmtree(stage)
                    except OSError:
                        raise EvaluationInputError() from None

    def upload(self, filename: str, payload: bytes, *, role: str,
               reading: ReadingLabel | None = None, variant: str | None = None,
               label: str | None = None) -> UploadedInput:
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or len(name) > 200 or Path(name).suffix.lower() != ".txt":
            raise EvaluationInputError("Izvēlieties .txt failu.", code="invalid")
        decode_text(payload, ground_truth=role == "ground_truth")
        record = UploadedInput(role=role, filename=name, label=label or name,
            reading=reading, variant=variant, original_sha256=sha256(payload).hexdigest(),
            original_size_bytes=len(payload))
        self._publish(self.inputs_root, record.id, {
            "input.txt": payload, "metadata.json": record.model_dump_json(indent=2).encode("utf-8")})
        return record

    def get_upload(self, input_id: UUID) -> UploadedInput:
        try:
            record = UploadedInput.model_validate_json(
                self._path(self.inputs_root, input_id, "metadata.json").read_bytes())
            if record.id != input_id:
                raise ValueError("Mismatched identity")
            return record
        except FileNotFoundError:
            raise EvaluationInputError("Izvēlētais teksta fails nav atrasts.", code="not_found") from None
        except (OSError, ValueError):
            raise EvaluationInputError() from None

    def list_uploads(self) -> tuple[UploadedInput, ...]:
        if not self.inputs_root.exists():
            return ()
        try:
            ids = sorted(UUID(path.name) for path in self.inputs_root.iterdir()
                         if path.is_dir() and not path.name.startswith(".pending-"))
        except (OSError, ValueError):
            raise EvaluationInputError() from None
        return tuple(self.get_upload(identifier) for identifier in ids)

    def _resolve(self, selection, *, ground_truth=False):
        if isinstance(selection, UploadedSelection):
            record = self.get_upload(selection.input_id)
            required = "ground_truth" if ground_truth else "hypothesis"
            if record.role != required:
                raise EvaluationInputError("Izvēlētais fails neatbilst vērtēšanas lomai.", code="invalid")
            payload = self._read(self._path(self.inputs_root, record.id, "input.txt"))
            info = dict(source=selection, role=record.role, label=record.label,
                        reading=record.reading, variant=record.variant)
        else:
            checkpoint = self.transcripts.load(selection.run_id)
            reading, attempt = self.transcripts._attempt(checkpoint.run, selection.attempt_id)
            path = self.transcripts.export_path(selection.run_id, selection.attempt_id,
                                                "editable" if selection.variant == "edited" else "raw")
            payload = self._read(path)
            if selection.variant == "raw":
                state = next(s for s in checkpoint.attempts if s.attempt_id == attempt.id)
                if sha256(payload).hexdigest() != state.raw_sha256:
                    raise EvaluationInputError()
            model = next(m for m in checkpoint.run.models if m.entry_id == attempt.model_entry_id)
            info = dict(source=selection, role="hypothesis", label=model.display_name,
                        reading=reading.label, variant=selection.variant,
                        model=model, processing_status=attempt.status)
        decode_text(payload, ground_truth=ground_truth)
        return info, payload

    def create_snapshot(self, request: EvaluationRequest) -> EvaluationSnapshot:
        import unicodedata
        reference_info, reference = self._resolve(request.ground_truth, ground_truth=True)
        resolved = [self._resolve(selection) for selection in request.hypotheses]
        readings = {info["reading"] for info, _ in resolved}
        variants = {info["variant"] for info, _ in resolved}
        if len(readings) != 1 or len(variants) != 1:
            raise EvaluationInputError("Vērtējiet A/D un oriģinālos/labotos failus atsevišķi.", code="invalid")
        files = {"ground_truth.txt": reference}
        def snapshot(info, payload, filename):
            return InputSnapshot(**info, file=filename, sha256=sha256(payload).hexdigest(),
                                 size_bytes=len(payload))
        hypotheses = []
        for index, (info, payload) in enumerate(resolved):
            filename = f"inputs/{index:03d}.txt"
            files[filename] = payload
            hypotheses.append(snapshot(info, payload, filename))
        result = EvaluationSnapshot(reading=next(iter(readings)), variant=next(iter(variants)),
            configuration=request.configuration, python_version=platform.python_version(),
            unicode_version=unicodedata.unidata_version, jiwer_version=version("jiwer"),
            ground_truth=snapshot(reference_info, reference, "ground_truth.txt"),
            hypotheses=tuple(hypotheses))
        files["metadata.json"] = result.model_dump_json(indent=2).encode("utf-8")
        self._publish(self.root, result.id, files)
        return result

    def load(self, evaluation_id: UUID) -> EvaluationSnapshot:
        try:
            result = EvaluationSnapshot.model_validate_json(
                self._path(self.root, evaluation_id, "metadata.json").read_bytes())
            if result.id != evaluation_id:
                raise ValueError("Mismatched identity")
            expected = ["ground_truth.txt", *(f"inputs/{i:03d}.txt" for i in range(len(result.hypotheses)))]
            for item, filename in zip((result.ground_truth, *result.hypotheses), expected):
                if item.file != filename:
                    raise ValueError("Unexpected file path")
                payload = self._read(self._path(self.root, evaluation_id, filename))
                if len(payload) != item.size_bytes or sha256(payload).hexdigest() != item.sha256:
                    raise ValueError("Snapshot changed")
                decode_text(payload, ground_truth=item.role == "ground_truth")
            return result
        except FileNotFoundError:
            raise EvaluationInputError("Vērtēšanas momentuzņēmums nav atrasts.", code="not_found") from None
        except (OSError, ValueError):
            raise EvaluationInputError() from None

    def read_snapshot_texts(self, evaluation_id: UUID) -> tuple[str, tuple[str, ...]]:
        snapshot = self.load(evaluation_id)
        texts = []
        for item in (snapshot.ground_truth, *snapshot.hypotheses):
            payload = self._read(self._path(self.root, evaluation_id, item.file))
            if len(payload) != item.size_bytes or sha256(payload).hexdigest() != item.sha256:
                raise EvaluationInputError()
            texts.append(decode_text(payload, ground_truth=item.role == "ground_truth"))
        return texts[0], tuple(texts[1:])
