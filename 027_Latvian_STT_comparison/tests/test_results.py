"""Offline aggregate result/history/export checks with real snapshot services."""

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import ReadingLabel
from app.diagnostics import DiagnosticStore
from app.evaluation_inputs import EvaluationInputError, EvaluationInputStore, EvaluationRequest, UploadedSelection
from app.main import create_app
from app.results import ResultsStore
from app.scoring import LegacyScoringStore
from app.transcripts import TranscriptStore


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = EvaluationInputStore(self.root / "data" / "evaluation_inputs",
            self.root / "evaluations", TranscriptStore(self.root / "runs"))
        self.store = ResultsStore(self.inputs, LegacyScoringStore(self.inputs), DiagnosticStore(self.inputs))

    def snapshot(self, *, label="Rīga", reference="A B", hypothesis="a B", reading=ReadingLabel.A, variant="raw"):
        gt = self.inputs.upload("gt.txt", reference.encode("utf-8"), role="ground_truth")
        hyp = self.inputs.upload("hyp.txt", hypothesis.encode("utf-8"), role="hypothesis",
            reading=reading, variant=variant, label=label)
        request = EvaluationRequest(ground_truth=UploadedSelection(input_id=gt.id),
            hypotheses=(UploadedSelection(input_id=hyp.id),))
        return self.inputs.create_snapshot(request), request, hyp

    def test_json_csv_exact_identity_na_units_and_idempotence(self):
        snapshot, _, _ = self.snapshot(label='Latvija, "ā"\nmodelis')
        result = self.store.calculate(snapshot.id)
        self.assertEqual(result["snapshot"]["id"], str(snapshot.id))
        self.assertEqual(result["texts"]["strict_reference"], "A B")
        self.assertTrue(Path(result["paths"]["inputs"][0]["source"]).is_file())
        self.assertTrue(Path(result["paths"]["inputs"][1]["snapshot"]).is_file())
        json_path = self.store.export_path(snapshot.id, "json")
        csv_path = self.store.export_path(snapshot.id, "csv")
        before = json_path.read_bytes(), csv_path.read_bytes()
        exported = json.loads(before[0])
        self.assertNotIn("paths", exported)
        self.assertEqual(exported["snapshot"]["hypotheses"][0]["sha256"], snapshot.hypotheses[0].sha256)
        rows = list(csv.DictReader(io.StringIO(before[1].decode("utf-8-sig"))))
        self.assertEqual(rows[0]["label"], 'Latvija, "ā"\nmodelis')
        self.assertEqual(rows[0]["punctuation_f1"], "")
        self.assertEqual(rows[0]["case_accuracy"], "0.5")
        self.assertEqual(rows[0]["evaluation_id"], str(snapshot.id))
        self.assertEqual(self.store.calculate(snapshot.id), result)
        self.assertEqual((json_path.read_bytes(), csv_path.read_bytes()), before)

    def test_edits_produce_new_history_and_old_exports_survive_source_deletion(self):
        first, request, hyp = self.snapshot(hypothesis="a b")
        old = self.store.calculate(first.id)
        source = self.inputs.inputs_root / str(hyp.id) / "input.txt"
        source.write_bytes(b"")
        second = self.inputs.create_snapshot(request)
        new = self.store.calculate(second.id)
        self.assertEqual(old["legacy"]["scores"][0]["wer"]["rate"], 0)
        self.assertEqual(new["legacy"]["scores"][0]["wer"]["rate"], 1)
        source.unlink()
        self.assertEqual(self.store.bundle(first.id)["legacy"], old["legacy"])
        self.assertTrue(self.store.export_path(first.id, "json").is_file())
        self.assertEqual(len(self.store.history()), 2)
        self.assertTrue(all(row["complete"] for row in self.store.history()))

    def test_partial_export_write_is_recoverable_and_history_is_not_complete(self):
        snapshot, _, _ = self.snapshot()
        from app.results import _write_atomic
        def fail_csv(path, payload, **kwargs):
            if path.name == "results.csv":
                raise PermissionError("synthetic-secret")
            return _write_atomic(path, payload, **kwargs)
        with patch("app.results._write_atomic", side_effect=fail_csv):
            with self.assertRaises(EvaluationInputError) as caught:
                self.store.calculate(snapshot.id)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertFalse(self.store.history()[0]["complete"])
        self.store.calculate(snapshot.id)
        self.assertTrue(self.store.history()[0]["complete"])

    def test_changed_exports_are_rejected_without_overwrite(self):
        snapshot, _, _ = self.snapshot()
        self.store.calculate(snapshot.id)
        path = self.store.export_path(snapshot.id, "json")
        path.write_bytes(b"changed")
        with self.assertRaises(EvaluationInputError):
            self.store.export_path(snapshot.id, "json")
        with self.assertRaises(EvaluationInputError):
            self.store.calculate(snapshot.id)
        self.assertEqual(path.read_bytes(), b"changed")

    def test_corrupt_snapshot_does_not_hide_other_history(self):
        first, _, _ = self.snapshot()
        second, _, _ = self.snapshot(reading=ReadingLabel.D, variant="edited")
        self.store.calculate(first.id)
        (self.inputs.root / str(second.id) / "ground_truth.txt").write_bytes(b"changed")
        history = self.store.history()
        self.assertEqual(len(history), 2)
        self.assertTrue(next(row for row in history if row["id"] == str(first.id))["valid"])
        self.assertFalse(next(row for row in history if row["id"] == str(second.id))["valid"])

    def test_csv_formula_label_inert_and_json_label_preserved(self):
        snapshot, _, _ = self.snapshot(label="=SUM(1,2)")
        self.store.calculate(snapshot.id)
        rows = list(csv.DictReader(io.StringIO(self.store.export_path(snapshot.id, "csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual(rows[0]["label"], "'=SUM(1,2)")
        exported = json.loads(self.store.export_path(snapshot.id, "json").read_bytes())
        self.assertEqual(exported["snapshot"]["hypotheses"][0]["label"], "=SUM(1,2)")

    def test_api_history_bundle_downloads_and_catalog(self):
        settings = Settings(self.root / "api")
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get("/api/evaluations").json(), [])
            self.assertEqual(client.get("/api/evaluation-catalog").json()["managed"], [])
            inputs = client.app.state.evaluation_inputs
            gt = inputs.upload("gt.txt", b"A", role="ground_truth")
            hyp = inputs.upload("hyp.txt", b"A B C", role="hypothesis", reading=ReadingLabel.D, variant="edited")
            snapshot = inputs.create_snapshot(EvaluationRequest(ground_truth=UploadedSelection(input_id=gt.id),
                hypotheses=(UploadedSelection(input_id=hyp.id),)))
            route = f"/api/evaluations/{snapshot.id}"
            self.assertEqual(client.get(route + "/results").status_code, 404)
            response = client.post(route + "/results")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["legacy"]["scores"][0]["wer"]["rate"], 2)
            self.assertEqual(client.get(route + "/results").json(), response.json())
            for kind in ("json", "csv"):
                export = client.get(route + "/exports/" + kind)
                self.assertEqual(export.status_code, 200)
                self.assertIn("attachment", export.headers["content-disposition"])
            self.assertEqual(client.get(route + "/exports/private.txt").status_code, 422)
            self.assertEqual(len(client.get("/api/evaluation-catalog").json()["uploads"]), 2)
            self.assertTrue(client.get("/api/evaluations").json()[0]["complete"])
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get(route + "/results").json(), response.json())


if __name__ == "__main__":
    unittest.main()
