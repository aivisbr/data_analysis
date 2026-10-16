"""Legacy compatibility, exact edit counts and durable offline scoring."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.contracts import ReadingLabel
from app.evaluation_inputs import EvaluationInputError, EvaluationInputStore, EvaluationRequest, UploadedSelection
from app.main import create_app
from app.scoring import EditMetric, LegacyScoringStore, normalize_legacy, score_legacy
from app.transcripts import TranscriptStore
from scripts.verify_legacy_scoring import original_normalize, verify_examples


class LegacyScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = EvaluationInputStore(self.root / "data" / "evaluation_inputs",
            self.root / "evaluations", TranscriptStore(self.root / "runs"))
        self.scoring = LegacyScoringStore(self.inputs)

    def snapshot(self, reference="Rīga, mūsu mājas!", hypothesis="Rīga mūsu mājas", variant="raw"):
        gt = self.inputs.upload("gt.txt", reference.encode("utf-8"), role="ground_truth")
        hyp = self.inputs.upload("hyp.txt", hypothesis.encode("utf-8"), role="hypothesis",
                                 reading=ReadingLabel.A, variant=variant)
        request = EvaluationRequest(ground_truth=UploadedSelection(input_id=gt.id),
                                    hypotheses=(UploadedSelection(input_id=hyp.id),))
        return self.inputs.create_snapshot(request), request, hyp

    def test_all_19_examples_match_original_normalization_and_whole_document_rates(self):
        report = verify_examples()
        self.assertEqual(report["matched_files"], 19)
        for item in report["scores"]:
            for name in ("wer", "cer"):
                metric = item[name]
                self.assertEqual(metric["rate"], (metric["substitutions"] + metric["deletions"] +
                                                metric["insertions"]) / metric["reference_length"])

    def test_normalization_matches_original_including_unicode_and_internal_hyphens(self):
        original = original_normalize()
        cases = ("\tĀČĒĢĪĶĻŅŠŪŽ, 12_34\r\nA-B!\u00a0C", "a\u0304 Ā", "İ RĪGA",
                 "Raimonds’ vārds...", "\n", "---", "123 foo_bar")
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(normalize_legacy(text), original(text))
        self.assertEqual(normalize_legacy(cases[0]), "āčēģīķļņšūž 12_34 a b c")
        # Legacy does not perform NFC; adding it would change scores for combining marks.
        self.assertEqual(normalize_legacy("a\u0304 Ā"), "a ā")

    def test_case_punctuation_whitespace_are_ignored_but_diacritics_preserved(self):
        wer, cer = score_legacy("RĪGA,\r\nLatvija!", "rīga\tlatvija")
        self.assertEqual((wer.rate, cer.rate), (0, 0))
        wer, cer = score_legacy("ā č ķ ļ ņ š ū ž", "a c k l n s u z")
        self.assertEqual(wer.rate, 1)
        self.assertEqual(wer.substitutions, 8)
        self.assertEqual(cer.substitutions, 8)
        self.assertEqual(cer.reference_length, 15)

    def test_hand_checked_word_substitution_deletion_insertion_counts(self):
        for hypothesis, counts in (("a x c", (2, 1, 0, 0)), ("a c", (2, 0, 1, 0)),
                                   ("a b c d", (3, 0, 0, 1))):
            wer, _ = score_legacy("a b c", hypothesis)
            self.assertEqual((wer.hits, wer.substitutions, wer.deletions, wer.insertions), counts)
            self.assertEqual(wer.rate, 1 / 3)

    def test_empty_hypothesis_is_all_deletions_and_cer_includes_spaces(self):
        wer, cer = score_legacy("ā b", "")
        self.assertEqual((wer.rate, wer.deletions, wer.hypothesis_length), (1, 2, 0))
        self.assertEqual((cer.rate, cer.deletions, cer.reference_length), (1, 3, 3))
        _, space = score_legacy("a b", "ab")
        self.assertEqual((space.rate, space.deletions, space.reference_length), (1 / 3, 1, 3))
        wer, cer = score_legacy("Text", "...!")
        self.assertEqual((wer.rate, cer.rate), (1, 1))

    def test_insertions_can_exceed_100_percent_without_accuracy_field(self):
        wer, cer = score_legacy("a", "a b c")
        self.assertEqual((wer.rate, wer.insertions), (2, 2))
        self.assertEqual((cer.rate, cer.insertions), (4, 4))
        self.assertNotIn("accuracy", wer.model_dump())
        self.assertNotIn("accuracy", cer.model_dump())

    def test_reference_empty_after_legacy_normalization_rejected(self):
        for reference in ("", " \n", "... — !!!"):
            with self.assertRaises(EvaluationInputError) as caught:
                score_legacy(reference, "Speech")
            self.assertEqual(caught.exception.code, "invalid")
        saved, _, _ = self.snapshot(reference="!!!")
        with self.assertRaises(EvaluationInputError):
            self.scoring.score(saved.id)
        self.assertFalse((self.root / "evaluations" / str(saved.id) / "legacy_scores.json").exists())

    def test_results_persist_with_versions_and_exact_snapshot_links(self):
        snapshot, _, _ = self.snapshot()
        scored = self.scoring.score(snapshot.id)
        self.assertEqual(scored.snapshot, snapshot)
        self.assertEqual(scored.scorer_version, "legacy-wer-cer-v1")
        self.assertEqual(scored.jiwer_version, "4.0.0")
        self.assertTrue(scored.rapidfuzz_version)
        self.assertEqual(scored.scores[0].input_sha256, snapshot.hypotheses[0].sha256)
        self.assertEqual(scored.scores[0].wer.rate, 0)
        self.assertNotIn("sentence", scored.model_dump_json())
        result_file = self.root / "evaluations" / str(snapshot.id) / "legacy_scores.json"
        first_bytes = result_file.read_bytes()
        self.scoring = LegacyScoringStore(self.inputs)
        self.assertEqual(self.scoring.load(snapshot.id), scored)
        self.assertEqual(self.scoring.score(snapshot.id), scored)
        self.assertEqual(result_file.read_bytes(), first_bytes)

    def test_source_edits_change_new_scores_without_changing_old_results(self):
        first, request, hyp = self.snapshot(variant="edited")
        before = self.scoring.score(first.id)
        (self.inputs.inputs_root / str(hyp.id) / "input.txt").write_bytes(b"")
        second = self.inputs.create_snapshot(request)
        after = self.scoring.score(second.id)
        self.assertEqual(before.scores[0].wer.rate, 0)
        self.assertEqual(after.scores[0].wer.rate, 1)
        self.assertEqual(self.scoring.load(first.id), before)
        self.assertEqual(after.snapshot.variant, "edited")

    def test_disk_failure_does_not_return_saved_score_or_change_input_snapshot(self):
        snapshot, _, _ = self.snapshot()
        with patch("app.scoring._write_atomic", side_effect=PermissionError("synthetic-secret")):
            with self.assertRaises(EvaluationInputError) as caught:
                self.scoring.score(snapshot.id)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(self.inputs.load(snapshot.id), snapshot)
        self.assertFalse((self.root / "evaluations" / str(snapshot.id) / "legacy_scores.json").exists())
        self.assertEqual(self.scoring.score(snapshot.id).scores[0].wer.rate, 0)

    def test_saved_results_and_snapshot_integrity_are_validated(self):
        snapshot, _, _ = self.snapshot()
        self.scoring.score(snapshot.id)
        path = self.root / "evaluations" / str(snapshot.id) / "legacy_scores.json"
        content = json.loads(path.read_bytes())
        content["scores"][0]["input_sha256"] = "0" * 64
        path.write_text(json.dumps(content), encoding="utf-8")
        with self.assertRaises(EvaluationInputError):
            self.scoring.load(snapshot.id)
        with self.assertRaises(ValidationError):
            EditMetric(rate=0, reference_length=1, hypothesis_length=0,
                       hits=0, substitutions=0, deletions=1, insertions=0)

    def test_api_scores_are_durable_and_normalized_empty_reference_returns_422(self):
        settings = Settings(self.root / "api")
        with TestClient(create_app(settings)) as client:
            store = client.app.state.evaluation_inputs
            gt = store.upload("gt.txt", b"a", role="ground_truth")
            hyp = store.upload("hyp.txt", b"a b c", role="hypothesis", reading=ReadingLabel.D, variant="raw")
            snapshot = store.create_snapshot(EvaluationRequest(ground_truth=UploadedSelection(input_id=gt.id),
                hypotheses=(UploadedSelection(input_id=hyp.id),)))
            route = f"/api/evaluations/{snapshot.id}/legacy-scores"
            self.assertEqual(client.get(route).status_code, 404)
            response = client.post(route)
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["scores"][0]["wer"]["rate"], 2)
            self.assertEqual(result["snapshot"]["reading"], "D")
            self.assertEqual(client.get(route).json(), result)
            empty = store.upload("gt.txt", b"!!!", role="ground_truth")
            empty_snapshot = store.create_snapshot(EvaluationRequest(ground_truth=UploadedSelection(input_id=empty.id),
                hypotheses=(UploadedSelection(input_id=hyp.id),)))
            self.assertEqual(client.post(f"/api/evaluations/{empty_snapshot.id}/legacy-scores").status_code, 422)
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get(route).json(), result)


if __name__ == "__main__":
    unittest.main()
