"""Whole-document legacy WER/CER and immutable results tied to input snapshots."""

from datetime import datetime
from importlib.metadata import version
import platform
import re
from threading import RLock
from typing import Literal
import unicodedata
from uuid import UUID

import jiwer
from pydantic import Field, model_validator

from app.contracts import Record, utc_now
from app.evaluation_inputs import EvaluationInputError, EvaluationInputStore, EvaluationSnapshot
from app.transcripts import _write_atomic


SCORER_VERSION = "legacy-wer-cer-v1"
RESULT_FILE = "legacy_scores.json"


def normalize_legacy(text: str) -> str:
    r"""Match example/evaluate_cli.py normalize(..., keep_diacritics=True).

    Do not add NFC, accent stripping, casefolding or alternative tokenization.
    Unicode \w deliberately preserves digits and underscores.
    """
    text = " ".join(text.split())
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


class EditMetric(Record):
    rate: float = Field(ge=0, allow_inf_nan=False)
    reference_length: int = Field(ge=1)
    hypothesis_length: int = Field(ge=0)
    hits: int = Field(ge=0)
    substitutions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    insertions: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_counts(self):
        if self.hits + self.substitutions + self.deletions != self.reference_length:
            raise ValueError("Etalona vienību skaits neatbilst kļūdu skaitiem.")
        if self.hits + self.substitutions + self.insertions != self.hypothesis_length:
            raise ValueError("Transkripta vienību skaits neatbilst kļūdu skaitiem.")
        expected = (self.substitutions + self.deletions + self.insertions) / self.reference_length
        if abs(self.rate - expected) > 1e-12:
            raise ValueError("Kļūdu īpatsvars neatbilst kļūdu skaitiem.")
        return self


class LegacyScore(Record):
    input_index: int = Field(ge=0)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wer: EditMetric
    cer: EditMetric


class LegacyResults(Record):
    schema_version: Literal[1] = 1
    scorer_version: Literal["legacy-wer-cer-v1"] = SCORER_VERSION
    scored_at: datetime = Field(default_factory=utc_now)
    snapshot: EvaluationSnapshot
    jiwer_version: str
    rapidfuzz_version: str
    python_version: str
    unicode_version: str
    scores: tuple[LegacyScore, ...]

    @model_validator(mode="after")
    def valid_links(self):
        if tuple(score.input_index for score in self.scores) != tuple(range(len(self.snapshot.hypotheses))):
            raise ValueError("Rezultātu secība neatbilst ievades failiem.")
        if any(score.input_sha256 != item.sha256 for score, item in zip(self.scores, self.snapshot.hypotheses)):
            raise ValueError("Rezultātu ievades kontrolsummas neatbilst.")
        if len({(s.wer.reference_length, s.cer.reference_length) for s in self.scores}) != 1:
            raise ValueError("Etalona garumam visiem transkriptiem jābūt vienādam.")
        return self


def score_legacy(reference: str, hypothesis: str) -> tuple[EditMetric, EditMetric]:
    ref = normalize_legacy(reference)
    hyp = normalize_legacy(hypothesis)
    if not ref:
        raise EvaluationInputError("Etalona teksts pēc normalizācijas ir tukšs.", code="invalid")
    words = jiwer.process_words(ref, hyp)
    characters = jiwer.process_characters(ref, hyp)

    def metric(output, rate, ref_length, hyp_length):
        return EditMetric(rate=rate, reference_length=ref_length, hypothesis_length=hyp_length,
            hits=output.hits, substitutions=output.substitutions,
            deletions=output.deletions, insertions=output.insertions)

    return (metric(words, words.wer, len(ref.split()), len(hyp.split())),
            metric(characters, characters.cer, len(ref), len(hyp)))


class LegacyScoringStore:
    def __init__(self, inputs: EvaluationInputStore):
        self.inputs = inputs
        self._lock = RLock()

    def _path(self, evaluation_id: UUID):
        return self.inputs._path(self.inputs.root, evaluation_id, RESULT_FILE)

    def score(self, evaluation_id: UUID) -> LegacyResults:
        """Score immutable inputs; new source edits require a new T007 snapshot."""
        with self._lock:
            snapshot = self.inputs.load(evaluation_id)
            if self._path(evaluation_id).exists():
                return self.load(evaluation_id)
            reference, hypotheses = self.inputs.read_snapshot_texts(evaluation_id)
            scores = []
            for index, text in enumerate(hypotheses):
                wer, cer = score_legacy(reference, text)
                scores.append(LegacyScore(input_index=index,
                    input_sha256=snapshot.hypotheses[index].sha256, wer=wer, cer=cer))
            result = LegacyResults(snapshot=snapshot, scores=tuple(scores),
                jiwer_version=version("jiwer"), rapidfuzz_version=version("rapidfuzz"),
                python_version=platform.python_version(), unicode_version=unicodedata.unidata_version)
            try:
                _write_atomic(self._path(evaluation_id), result.model_dump_json(indent=2).encode("utf-8"),
                              exclusive=True)
            except OSError:
                raise EvaluationInputError("Vērtēšanas rezultātus nevar saglabāt.") from None
            return result

    def load(self, evaluation_id: UUID) -> LegacyResults:
        with self._lock:
            snapshot = self.inputs.load(evaluation_id)
            try:
                result = LegacyResults.model_validate_json(self._path(evaluation_id).read_bytes())
                if result.snapshot != snapshot:
                    raise ValueError("Snapshot mismatch")
                return result
            except FileNotFoundError:
                raise EvaluationInputError("Šim momentuzņēmumam vēl nav WER/CER rezultātu.", code="not_found") from None
            except (OSError, ValueError):
                raise EvaluationInputError("Saglabātos WER/CER rezultātus nevar nolasīt.") from None
