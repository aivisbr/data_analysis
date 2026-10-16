"""Hand-checked punctuation placement, lexical anchors, case and strict text tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.contracts import ReadingLabel
from app.diagnostics import DiagnosticStore, diagnose, normalize_strict, tokenize
from app.evaluation_inputs import EvaluationInputError, EvaluationInputStore, EvaluationRequest, UploadedSelection
from app.main import create_app
from app.scoring import LegacyScoringStore, score_legacy
from app.transcripts import TranscriptStore


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = EvaluationInputStore(self.root / "data" / "evaluation_inputs",
            self.root / "evaluations", TranscriptStore(self.root / "runs"))
        self.store = DiagnosticStore(self.inputs)

    def snapshot(self, reference="Rīga, mūsu mājas!", hypothesis="rīga mūsu mājas."):
        gt = self.inputs.upload("gt.txt", reference.encode("utf-8"), role="ground_truth")
        hyp = self.inputs.upload("hyp.txt", hypothesis.encode("utf-8"), role="hypothesis",
                                 reading=ReadingLabel.A, variant="edited")
        request = EvaluationRequest(ground_truth=UploadedSelection(input_id=gt.id),
                                    hypotheses=(UploadedSelection(input_id=hyp.id),))
        return self.inputs.create_snapshot(request), request, hyp

    def counts(self, diagnostic):
        t = diagnostic.punctuation.totals
        return t.true_positive, t.false_positive, t.false_negative

    def test_shifted_comma_is_not_correct_despite_equal_mark_counts(self):
        result = diagnose("A, B C.", "A B, C.")
        self.assertEqual(self.counts(result), (1, 1, 1))
        self.assertEqual(result.punctuation.totals.f1, 0.5)
        comma = next(p for p in result.punctuation.per_mark if p.mark == ",")
        self.assertEqual((comma.true_positive, comma.false_positive, comma.false_negative), (0, 1, 1))
        self.assertEqual({issue.reference_boundary for issue in result.punctuation.issues}, {1, 2})

    def test_wrong_mark_is_one_missing_and_one_extra(self):
        result = diagnose("Sveiki!", "Sveiki?")
        self.assertEqual(self.counts(result), (0, 1, 1))
        self.assertEqual(result.punctuation.totals.f1, 0)
        self.assertEqual(result.punctuation.issues[0].expected, "!")
        self.assertEqual(result.punctuation.issues[0].actual, "?")

    def test_quotes_brackets_start_end_and_mark_order(self):
        text = '„Rīga“ (Latvija): jā; tiešām?!'
        exact = diagnose(text, text)
        self.assertTrue(exact.exact_match)
        self.assertEqual(exact.punctuation.totals.f1, 1)
        self.assertEqual(exact.punctuation.issues, ())
        swapped = diagnose("A?!", "A!?")
        self.assertEqual(self.counts(swapped), (1, 1, 1))
        start = diagnose('"A"', 'A"')
        self.assertEqual(self.counts(start), (1, 0, 1))

    def test_inserted_deleted_substituted_words_make_adjacent_boundaries_unmatched(self):
        for reference, hypothesis in (("A, B.", "A, X B."), ("A, X B.", "A, B."),
                                      ("A, B.", "A, C.")):
            with self.subTest(hypothesis=hypothesis):
                result = diagnose(reference, hypothesis)
                comma = next(p for p in result.punctuation.per_mark if p.mark == ",")
                self.assertEqual((comma.true_positive, comma.false_positive, comma.false_negative), (0, 1, 1))
        # Matching word adjacency after an inserted prefix still establishes an interior anchor.
        self.assertEqual(self.counts(diagnose("A, B.", "X A, B.")), (2, 0, 0))

    def test_repeated_words_have_reproducible_alignment_and_explicit_flag(self):
        first = diagnose("A, A B.", "A A, B.")
        self.assertTrue(first.has_repeated_lexical_tokens)
        self.assertEqual(self.counts(first), (1, 1, 1))
        self.assertEqual(diagnose("A, A B.", "A A, B."), first)
        missing = diagnose("A, A B.", "A B.")
        self.assertTrue(missing.has_repeated_lexical_tokens)
        self.assertEqual(diagnose("A, A B.", "A B."), missing)

    def test_exact_typography_has_no_hidden_quote_dash_ellipsis_equivalence(self):
        result = diagnose('"A-B..."', '“A–B…”')
        self.assertEqual(self.counts(result), (0, 4, 4))
        self.assertFalse(result.exact_match)
        self.assertGreater(result.strict_cer.rate, 0)
        marks = {p.mark for p in result.punctuation.per_mark}
        self.assertTrue({'"', '“', '”', '-', '–', '...', '…'} <= marks)

    def test_internal_apostrophe_hyphen_digits_underscore_and_symbols_policy(self):
        words, boundaries = tokenize(normalize_strict("O’Neil A-B 12_3 €"))
        self.assertEqual(words, ["O", "Neil", "A", "B", "12_3"])
        self.assertEqual([m for marks in boundaries for m in marks], ["’", "-"])
        symbols = diagnose("A €", "A $")
        self.assertIsNone(symbols.punctuation.totals.f1)
        self.assertGreater(symbols.strict_cer.rate, 0)

    def test_no_punctuation_zero_denominators_and_empty_hypothesis(self):
        absent = diagnose("A B", "A B")
        self.assertEqual((absent.punctuation.totals.precision, absent.punctuation.totals.recall,
                          absent.punctuation.totals.f1), (None, None, None))
        missing = diagnose("A.", "A")
        self.assertEqual((missing.punctuation.totals.precision, missing.punctuation.totals.recall,
                          missing.punctuation.totals.f1), (None, 0, None))
        extra = diagnose("A", "A!")
        self.assertEqual((extra.punctuation.totals.precision, extra.punctuation.totals.recall,
                          extra.punctuation.totals.f1), (0, None, None))
        empty = diagnose("A.", "")
        self.assertEqual(empty.strict_cer.rate, 1)
        self.assertEqual(self.counts(empty), (0, 0, 1))
        self.assertIsNone(empty.capitalization.accuracy)
        self.assertEqual(empty.capitalization.coverage, 0)

    def test_punctuation_only_text_uses_single_start_end_boundary(self):
        result = diagnose("?!", "?!")
        self.assertTrue(result.exact_match)
        self.assertEqual(self.counts(result), (2, 0, 0))
        self.assertIsNone(result.capitalization.coverage)
        self.assertIsNone(result.capitalization.lexical_coverage)
        with self.assertRaises(EvaluationInputError):
            diagnose(" \n", "text")

    def test_case_is_conditional_and_reference_coverage_excludes_unmatched_words(self):
        result = diagnose("Rīga Latvija 123", "rīga Lietuva 123")
        case = result.capitalization
        self.assertEqual((case.correct, case.eligible, case.reference_cased_words), (0, 1, 2))
        self.assertEqual((case.accuracy, case.coverage), (0, 0.5))
        self.assertEqual((case.aligned_lexical_words, case.reference_words), (2, 3))
        self.assertEqual(case.lexical_coverage, 2 / 3)
        self.assertEqual(case.incorrect[0].expected, "Rīga")
        self.assertIsNone(diagnose("123", "123").capitalization.accuracy)
        self.assertEqual(score_legacy("Rīga Latvija", "rīga latvija")[0].rate, 0)

    def test_strict_cer_preserves_case_punctuation_and_only_normalizes_nfc_whitespace(self):
        result = diagnose("Ā,\r\nB!", "A b")
        self.assertEqual((result.strict_cer.reference_length, result.strict_cer.hypothesis_length), (5, 3))
        self.assertGreater(result.strict_cer.rate, 0)
        self.assertFalse(result.exact_match)
        nfc = diagnose("a\u0304\tB", "ā B")
        self.assertTrue(nfc.exact_match)
        self.assertEqual(nfc.strict_cer.rate, 0)
        self.assertEqual(score_legacy("a\u0304 B", "ā B")[0].rate, 0.5)
        self.assertEqual(diagnose("A", "A B C").strict_cer.rate, 4)

    def test_character_changes_reconstruct_strict_hypothesis(self):
        reference, hypothesis = "Rīga, Latvija!", "rīga Latvija?"
        result = diagnose(reference, hypothesis)
        rebuilt = normalize_strict(reference)
        for change in reversed(result.character_changes):
            self.assertEqual(rebuilt[change.reference_start:change.reference_end], change.expected)
            rebuilt = rebuilt[:change.reference_start] + change.actual + rebuilt[change.reference_end:]
        self.assertEqual(rebuilt, normalize_strict(hypothesis))

    def test_storage_versions_idempotence_and_legacy_output_not_modified(self):
        snapshot, request, source = self.snapshot()
        legacy = LegacyScoringStore(self.inputs).score(snapshot.id)
        legacy_path = self.inputs.root / str(snapshot.id) / "legacy_scores.json"
        before = legacy_path.read_bytes()
        result = self.store.score(snapshot.id)
        self.assertEqual(self.store.load(snapshot.id), result)
        self.assertEqual(self.store.score(snapshot.id), result)
        self.assertEqual(result.snapshot, snapshot)
        self.assertEqual(legacy_path.read_bytes(), before)
        self.assertEqual(LegacyScoringStore(self.inputs).load(snapshot.id), legacy)
        self.assertEqual(result.scorer_version, "text-diagnostics-v1")
        self.assertTrue(result.rapidfuzz_version)
        (self.inputs.inputs_root / str(source.id) / "input.txt").write_text("Rīga, mūsu mājas!", encoding="utf-8")
        newer = self.inputs.create_snapshot(request)
        self.assertTrue(self.store.score(newer.id).scores[0].diagnostics.exact_match)
        self.assertFalse(self.store.load(snapshot.id).scores[0].diagnostics.exact_match)

    def test_write_failure_and_corrupted_links_are_safe(self):
        snapshot, _, _ = self.snapshot()
        with patch("app.diagnostics._write_atomic", side_effect=PermissionError("synthetic-secret")):
            with self.assertRaises(EvaluationInputError) as caught:
                self.store.score(snapshot.id)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        result = self.store.score(snapshot.id)
        path = self.inputs.root / str(snapshot.id) / "diagnostics.json"
        content = json.loads(path.read_bytes())
        content["scores"][0]["input_sha256"] = "0" * 64
        path.write_text(json.dumps(content), encoding="utf-8")
        with self.assertRaises(EvaluationInputError):
            self.store.load(result.snapshot.id)

    def test_all_supplied_fixtures_produce_serializable_diagnostics_and_reconstruct_changes(self):
        reference = (PROJECT_ROOT / "example" / "LSMzinas_lav_GT.txt").read_text(encoding="utf-8-sig")
        for path in sorted((PROJECT_ROOT / "example" / "tests").rglob("*.txt")):
            with self.subTest(file=path.name):
                hypothesis = path.read_text(encoding="utf-8-sig")
                result = diagnose(reference, hypothesis)
                self.assertEqual(type(result).model_validate_json(result.model_dump_json()), result)
                rebuilt = normalize_strict(reference)
                for change in reversed(result.character_changes):
                    rebuilt = rebuilt[:change.reference_start] + change.actual + rebuilt[change.reference_end:]
                self.assertEqual(rebuilt, normalize_strict(hypothesis))

    def test_api_results_survive_restart_and_preserve_na_as_null(self):
        settings = Settings(self.root / "api")
        with TestClient(create_app(settings)) as client:
            store = client.app.state.evaluation_inputs
            gt = store.upload("gt.txt", b"A B", role="ground_truth")
            hyp = store.upload("hyp.txt", b"a B", role="hypothesis", reading=ReadingLabel.D, variant="raw")
            snapshot = store.create_snapshot(EvaluationRequest(ground_truth=UploadedSelection(input_id=gt.id),
                hypotheses=(UploadedSelection(input_id=hyp.id),)))
            route = f"/api/evaluations/{snapshot.id}/diagnostics"
            self.assertEqual(client.get(route).status_code, 404)
            response = client.post(route)
            self.assertEqual(response.status_code, 200)
            saved = response.json()
            diagnostic = saved["scores"][0]["diagnostics"]
            self.assertIsNone(diagnostic["punctuation"]["totals"]["f1"])
            self.assertEqual(diagnostic["capitalization"]["accuracy"], 0.5)
            self.assertEqual(saved["snapshot"]["reading"], "D")
            self.assertEqual(client.get(route).json(), saved)
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get(route).json(), saved)


if __name__ == "__main__":
    unittest.main()
