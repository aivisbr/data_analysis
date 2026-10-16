"""Project-defined strict text, anchored punctuation and conditional case metrics."""

from collections import Counter
from datetime import datetime
from importlib.metadata import version
import platform
from threading import RLock
from typing import Literal
import unicodedata
from uuid import UUID

import jiwer
from pydantic import Field, model_validator
from rapidfuzz.distance import Levenshtein

from app.contracts import Record, utc_now
from app.evaluation_inputs import EvaluationInputError, EvaluationInputStore, EvaluationSnapshot
from app.scoring import EditMetric
from app.transcripts import _write_atomic


def normalize_strict(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def tokenize(text: str):
    """NFC text: Unicode letters/numbers/marks/underscore words; punctuation gaps."""
    words, boundaries = [], [[]]
    index = 0
    while index < len(text):
        char = text[index]
        if char == "_" or unicodedata.category(char)[0] in "LNM":
            end = index + 1
            while end < len(text) and (text[end] == "_" or unicodedata.category(text[end])[0] in "LNM"):
                end += 1
            words.append(text[index:end])
            boundaries.append([])
            index = end
        elif text.startswith("...", index):
            boundaries[-1].append("...")
            index += 3
        else:
            if unicodedata.category(char).startswith("P"):
                boundaries[-1].append(char)
            index += 1
    return words, boundaries


class MarkCounts(Record):
    mark: str
    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    precision: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    recall: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    f1: float | None = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_rates(self):
        tp, fp, fn = self.true_positive, self.false_positive, self.false_negative
        expected = (tp / (tp + fp) if tp + fp else None, tp / (tp + fn) if tp + fn else None,
                    2 * tp / (2 * tp + fp + fn) if tp + fp and tp + fn else None)
        if (self.precision, self.recall, self.f1) != expected:
            raise ValueError("Pieturzīmju rādītāji neatbilst skaitiem.")
        return self


def mark_counts(mark, tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if precision is not None and recall is not None else None
    return MarkCounts(mark=mark, true_positive=tp, false_positive=fp, false_negative=fn,
                      precision=precision, recall=recall, f1=f1)


class PunctuationIssue(Record):
    reference_boundary: int | None
    hypothesis_boundary: int | None
    expected: str | None
    actual: str | None


class PunctuationMetrics(Record):
    totals: MarkCounts
    per_mark: tuple[MarkCounts, ...]
    issues: tuple[PunctuationIssue, ...]
    matched_boundaries: int
    reference_boundaries: int
    hypothesis_boundaries: int

    @model_validator(mode="after")
    def valid_counts(self):
        if len({p.mark for p in self.per_mark}) != len(self.per_mark) or any(
            getattr(self.totals, key) != sum(getattr(p, key) for p in self.per_mark)
            for key in ("true_positive", "false_positive", "false_negative")
        ):
            raise ValueError("Pieturzīmju kopsummas neatbilst.")
        if sum(issue.expected is not None for issue in self.issues) != self.totals.false_negative or sum(
            issue.actual is not None for issue in self.issues) != self.totals.false_positive:
            raise ValueError("Pieturzīmju atšķirības neatbilst skaitiem.")
        return self


class CaseIssue(Record):
    reference_index: int
    hypothesis_index: int
    expected: str
    actual: str


class CapitalizationMetrics(Record):
    correct: int
    eligible: int
    accuracy: float | None
    reference_cased_words: int
    coverage: float | None
    aligned_lexical_words: int
    reference_words: int
    lexical_coverage: float | None
    incorrect: tuple[CaseIssue, ...]

    @model_validator(mode="after")
    def valid_counts(self):
        if not 0 <= self.correct <= self.eligible <= self.reference_cased_words <= self.reference_words:
            raise ValueError("Burtu reģistra skaiti nav derīgi.")
        if len(self.incorrect) != self.eligible - self.correct or not 0 <= self.aligned_lexical_words <= self.reference_words:
            raise ValueError("Burtu reģistra atšķirības neatbilst skaitiem.")
        if (self.accuracy, self.coverage, self.lexical_coverage) != (
            self.correct / self.eligible if self.eligible else None,
            self.eligible / self.reference_cased_words if self.reference_cased_words else None,
            self.aligned_lexical_words / self.reference_words if self.reference_words else None
        ):
            raise ValueError("Burtu reģistra rādītāji neatbilst skaitiem.")
        return self


class CharacterChange(Record):
    operation: Literal["replace", "delete", "insert"]
    reference_start: int
    reference_end: int
    hypothesis_start: int
    hypothesis_end: int
    expected: str
    actual: str


class TextDiagnostics(Record):
    strict_cer: EditMetric
    exact_match: bool
    punctuation: PunctuationMetrics
    capitalization: CapitalizationMetrics
    has_repeated_lexical_tokens: bool
    character_changes: tuple[CharacterChange, ...]


def diagnose(reference: str, hypothesis: str) -> TextDiagnostics:
    ref, hyp = normalize_strict(reference), normalize_strict(hypothesis)
    if not ref:
        raise EvaluationInputError("Etalona teksts nedrīkst būt tukšs.", code="invalid")
    chars = jiwer.process_characters(ref, hyp)
    strict = EditMetric(rate=chars.cer, reference_length=len(ref), hypothesis_length=len(hyp),
        hits=chars.hits, substitutions=chars.substitutions, deletions=chars.deletions, insertions=chars.insertions)
    ref_words, ref_boundaries = tokenize(ref)
    hyp_words, hyp_boundaries = tokenize(hyp)
    ref_keys, hyp_keys = [w.lower() for w in ref_words], [w.lower() for w in hyp_words]
    # Unit-cost Levenshtein opcodes; only exact lexical equal runs create anchors.
    mapping = {}
    for op in Levenshtein.opcodes(ref_keys, hyp_keys):
        if op.tag == "equal":
            mapping.update(zip(range(op.src_start, op.src_end), range(op.dest_start, op.dest_end)))
    boundary_mapping = {}
    if not ref_words and not hyp_words:
        boundary_mapping[0] = 0
    else:
        if ref_words and hyp_words and mapping.get(0) == 0:
            boundary_mapping[0] = 0
        for boundary in range(1, len(ref_words)):
            left, right = mapping.get(boundary - 1), mapping.get(boundary)
            if left is not None and right == left + 1:
                boundary_mapping[boundary] = right
        if ref_words and hyp_words and mapping.get(len(ref_words) - 1) == len(hyp_words) - 1:
            boundary_mapping[len(ref_words)] = len(hyp_words)
    tp, fp, fn = Counter(), Counter(), Counter()
    issues = []
    for rb, hb in boundary_mapping.items():
        expected, actual = ref_boundaries[rb], hyp_boundaries[hb]
        for op in Levenshtein.opcodes(expected, actual):
            if op.tag == "equal":
                tp.update(expected[op.src_start:op.src_end])
            else:
                missing, extra = expected[op.src_start:op.src_end], actual[op.dest_start:op.dest_end]
                fn.update(missing)
                fp.update(extra)
                for index in range(max(len(missing), len(extra))):
                    issues.append(PunctuationIssue(reference_boundary=rb, hypothesis_boundary=hb,
                        expected=missing[index] if index < len(missing) else None,
                        actual=extra[index] if index < len(extra) else None))
    for rb, marks in enumerate(ref_boundaries):
        if rb not in boundary_mapping:
            fn.update(marks)
            issues.extend(PunctuationIssue(reference_boundary=rb, hypothesis_boundary=None,
                                           expected=mark, actual=None) for mark in marks)
    mapped_hyp = set(boundary_mapping.values())
    for hb, marks in enumerate(hyp_boundaries):
        if hb not in mapped_hyp:
            fp.update(marks)
            issues.extend(PunctuationIssue(reference_boundary=None, hypothesis_boundary=hb,
                                           expected=None, actual=mark) for mark in marks)
    per_mark = tuple(mark_counts(mark, tp[mark], fp[mark], fn[mark]) for mark in sorted(tp.keys() | fp.keys() | fn.keys()))
    punctuation = PunctuationMetrics(totals=mark_counts("all", sum(tp.values()), sum(fp.values()), sum(fn.values())),
        per_mark=per_mark, issues=tuple(issues), matched_boundaries=len(boundary_mapping),
        reference_boundaries=len(ref_boundaries), hypothesis_boundaries=len(hyp_boundaries))
    cased = lambda word: word.lower() != word.upper()
    eligible = [(ri, hi) for ri, hi in mapping.items() if cased(ref_words[ri])]
    case_issues = tuple(CaseIssue(reference_index=ri, hypothesis_index=hi,
        expected=ref_words[ri], actual=hyp_words[hi]) for ri, hi in eligible if ref_words[ri] != hyp_words[hi])
    reference_cased = sum(cased(word) for word in ref_words)
    correct = len(eligible) - len(case_issues)
    capitalization = CapitalizationMetrics(correct=correct, eligible=len(eligible),
        accuracy=correct / len(eligible) if eligible else None, reference_cased_words=reference_cased,
        coverage=len(eligible) / reference_cased if reference_cased else None,
        aligned_lexical_words=len(mapping), reference_words=len(ref_words),
        lexical_coverage=len(mapping) / len(ref_words) if ref_words else None, incorrect=case_issues)
    changes = tuple(CharacterChange(operation=op.tag, reference_start=op.src_start, reference_end=op.src_end,
        hypothesis_start=op.dest_start, hypothesis_end=op.dest_end,
        expected=ref[op.src_start:op.src_end], actual=hyp[op.dest_start:op.dest_end])
        for op in Levenshtein.opcodes(ref, hyp) if op.tag != "equal")
    return TextDiagnostics(strict_cer=strict, exact_match=ref == hyp, punctuation=punctuation,
        capitalization=capitalization, character_changes=changes,
        has_repeated_lexical_tokens=len(set(ref_keys)) < len(ref_keys) or len(set(hyp_keys)) < len(hyp_keys))


class DiagnosticScore(Record):
    input_index: int = Field(ge=0)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostics: TextDiagnostics


class DiagnosticResults(Record):
    schema_version: Literal[1] = 1
    scorer_version: Literal["text-diagnostics-v1"] = "text-diagnostics-v1"
    alignment_policy: Literal["rapidfuzz-equal-adjacent-anchors-v1"] = "rapidfuzz-equal-adjacent-anchors-v1"
    scored_at: datetime = Field(default_factory=utc_now)
    snapshot: EvaluationSnapshot
    jiwer_version: str
    rapidfuzz_version: str
    python_version: str
    unicode_version: str
    scores: tuple[DiagnosticScore, ...]

    @model_validator(mode="after")
    def valid_links(self):
        if tuple(s.input_index for s in self.scores) != tuple(range(len(self.snapshot.hypotheses))) or any(
            score.input_sha256 != item.sha256 for score, item in zip(self.scores, self.snapshot.hypotheses)
        ):
            raise ValueError("Diagnostikas rezultātu ievades neatbilst.")
        return self


class DiagnosticStore:
    def __init__(self, inputs: EvaluationInputStore):
        self.inputs = inputs
        self._lock = RLock()

    def _path(self, identifier):
        return self.inputs._path(self.inputs.root, identifier, "diagnostics.json")

    def score(self, evaluation_id: UUID) -> DiagnosticResults:
        with self._lock:
            snapshot = self.inputs.load(evaluation_id)
            if self._path(evaluation_id).exists():
                return self.load(evaluation_id)
            reference, hypotheses = self.inputs.read_snapshot_texts(evaluation_id)
            result = DiagnosticResults(snapshot=snapshot, jiwer_version=version("jiwer"),
                rapidfuzz_version=version("rapidfuzz"), python_version=platform.python_version(),
                unicode_version=unicodedata.unidata_version,
                scores=tuple(DiagnosticScore(input_index=i, input_sha256=snapshot.hypotheses[i].sha256,
                    diagnostics=diagnose(reference, text)) for i, text in enumerate(hypotheses)))
            try:
                _write_atomic(self._path(evaluation_id), result.model_dump_json(indent=2).encode("utf-8"), exclusive=True)
            except OSError:
                raise EvaluationInputError("Diagnostikas rezultātus nevar saglabāt.") from None
            return result

    def load(self, evaluation_id: UUID) -> DiagnosticResults:
        with self._lock:
            snapshot = self.inputs.load(evaluation_id)
            try:
                result = DiagnosticResults.model_validate_json(self._path(evaluation_id).read_bytes())
                if result.snapshot != snapshot:
                    raise ValueError("Snapshot mismatch")
                return result
            except FileNotFoundError:
                raise EvaluationInputError("Šim momentuzņēmumam vēl nav diagnostikas rezultātu.", code="not_found") from None
            except (OSError, ValueError):
                raise EvaluationInputError("Diagnostikas rezultātus nevar nolasīt.") from None
