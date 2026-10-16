"""Exact-byte snapshots and fresh disk reads, using real example fixtures offline."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.contracts import ReadingLabel, RunRecord, Status
from app.evaluation_inputs import (
    EvaluationInputError, EvaluationInputStore, EvaluationRequest, ManagedSelection,
    MAX_TEXT_BYTES, UploadedSelection,
)
from app.main import create_app
from app.transcripts import TranscriptStore, TranscriptStorageError
from test_transcripts import event, fixture


class EvaluationInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcripts = TranscriptStore(self.root / "runs")
        self.store = EvaluationInputStore(self.root / "data" / "evaluation_inputs",
                                         self.root / "evaluations", self.transcripts)

    def upload(self, payload=b"Riga.", *, ground_truth=False, reading=ReadingLabel.A, variant="raw"):
        return self.store.upload("reference.txt" if ground_truth else "hypothesis.txt", payload,
            role="ground_truth" if ground_truth else "hypothesis",
            reading=None if ground_truth else reading, variant=None if ground_truth else variant)

    def request(self, reference, *hypotheses):
        return EvaluationRequest(ground_truth=UploadedSelection(input_id=reference.id),
            hypotheses=tuple(UploadedSelection(input_id=item.id) for item in hypotheses))

    def test_all_supplied_examples_preserve_exact_bytes_without_modifying_originals(self):
        reference_path = PROJECT_ROOT / "example" / "LSMzinas_lav_GT.txt"
        original_reference = reference_path.read_bytes()
        reference = self.store.upload(reference_path.name, original_reference, role="ground_truth")
        paths = sorted((PROJECT_ROOT / "example" / "tests").rglob("*.txt"))
        self.assertGreater(len(paths), 0)
        originals = {path: path.read_bytes() for path in paths}
        hypotheses = [self.store.upload(path.name, content, role="hypothesis",
            reading=ReadingLabel.A, variant="raw", label=path.relative_to(PROJECT_ROOT / "example").as_posix())
            for path, content in originals.items()]
        saved = self.store.create_snapshot(self.request(reference, *hypotheses))
        self.assertEqual(saved.status, "inputs_saved")
        self.assertEqual(saved.scorer_version, "not-run")
        self.assertEqual(saved.jiwer_version, "4.0.0")
        self.assertEqual(saved.configuration.legacy_normalization, "evaluate-cli-v1")
        self.assertTrue(saved.configuration.preserve_diacritics)
        folder = self.root / "evaluations" / str(saved.id)
        self.assertEqual((folder / saved.ground_truth.file).read_bytes(), original_reference)
        for item, (path, original) in zip(saved.hypotheses, originals.items()):
            self.assertEqual((folder / item.file).read_bytes(), original)
            self.assertEqual(item.sha256, sha256(original).hexdigest())
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(reference_path.read_bytes(), original_reference)
        self.assertEqual(self.store.load(saved.id), saved)

    def test_uploaded_source_edits_change_new_snapshot_only_and_survive_restart(self):
        reference = self.upload(b"\xef\xbb\xbfReference\r\n", ground_truth=True)
        hypothesis = self.upload(b"First\r\n", variant="edited")
        request = self.request(reference, hypothesis)
        first = self.store.create_snapshot(request)
        source = self.root / "data" / "evaluation_inputs" / str(hypothesis.id) / "input.txt"
        source.write_bytes("Ārējs labojums!\r\n".encode("utf-8"))
        reference_source = self.root / "data" / "evaluation_inputs" / str(reference.id) / "input.txt"
        reference_source.write_bytes("Jauns etalons.\n".encode("utf-8"))
        self.store = EvaluationInputStore(self.store.inputs_root, self.store.root, self.transcripts)
        second = self.store.create_snapshot(request)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.hypotheses[0].sha256, second.hypotheses[0].sha256)
        self.assertNotEqual(first.ground_truth.sha256, second.ground_truth.sha256)
        self.assertEqual(self.store.read_snapshot_texts(first.id), ("Reference\r\n", ("First\r\n",)))
        self.assertEqual(self.store.read_snapshot_texts(second.id),
                         ("Jauns etalons.\n", ("Ārējs labojums!\r\n",)))
        self.assertEqual(len(self.store.list_uploads()), 2)

    def test_managed_edited_transcript_is_reread_and_identified(self):
        run = fixture()
        self.transcripts.checkpoint_run(run)
        self.transcripts.accept(event(run, "Oriģināls."))
        attempt = run.readings[0].attempts[0]
        self.transcripts.finalize(run.id, attempt.id, status=Status.COMPLETE)
        reference = self.upload("Etalons.".encode("utf-8"), ground_truth=True)
        selection = ManagedSelection(run_id=run.id, attempt_id=attempt.id, variant="edited")
        request = EvaluationRequest(ground_truth=UploadedSelection(input_id=reference.id), hypotheses=(selection,))
        first = self.store.create_snapshot(request)
        path = self.transcripts.export_path(run.id, attempt.id, "editable")
        path.write_bytes("Labots ārēji.\r\n".encode("utf-8"))
        second = self.store.create_snapshot(request)
        self.assertEqual(second.hypotheses[0].model, run.models[0])
        self.assertEqual(second.hypotheses[0].reading, ReadingLabel.A)
        self.assertEqual(second.hypotheses[0].variant, "edited")
        self.assertEqual(second.hypotheses[0].processing_status, Status.COMPLETE)
        self.assertEqual(self.store.read_snapshot_texts(first.id)[1], ("Oriģināls.",))
        self.assertEqual(self.store.read_snapshot_texts(second.id)[1], ("Labots ārēji.\r\n",))
        raw_request = request.evolve(hypotheses=(selection.evolve(variant="raw"),))
        raw = self.store.create_snapshot(raw_request)
        self.assertEqual(self.store.read_snapshot_texts(raw.id)[1], ("Oriģināls.",))

    def test_empty_ground_truth_rejected_on_import_and_after_disk_edit(self):
        for payload in (b"", b" \r\n\t", b"\xef\xbb\xbf"):
            with self.subTest(payload=payload), self.assertRaises(EvaluationInputError):
                self.upload(payload, ground_truth=True)
        reference = self.upload(b"Reference", ground_truth=True)
        hypothesis = self.upload(b"")
        # Empty failed hypothesis is valid; scoring its deletions follows T008.
        self.assertEqual(self.store.create_snapshot(self.request(reference, hypothesis)).hypotheses[0].size_bytes, 0)
        source = self.store.inputs_root / str(reference.id) / "input.txt"
        source.write_bytes(b" \n")
        with self.assertRaises(EvaluationInputError):
            self.store.create_snapshot(self.request(reference, hypothesis))

    def test_invalid_encoding_binary_extension_size_and_role(self):
        for payload in (b"\xff\xfe", b"text\x00binary", b"x" * (MAX_TEXT_BYTES + 1)):
            with self.assertRaises(EvaluationInputError):
                self.upload(payload)
        with self.assertRaises(EvaluationInputError):
            self.store.upload("audio.wav", b"text", role="ground_truth")
        with self.assertRaises(ValidationError):
            self.store.upload("hyp.txt", b"text", role="hypothesis")
        reference = self.upload(ground_truth=True)
        with self.assertRaises(EvaluationInputError):
            self.store.create_snapshot(self.request(reference, reference))

    def test_readings_and_raw_edited_variants_cannot_mix(self):
        reference = self.upload(ground_truth=True)
        first = self.upload()
        for other in (self.upload(reading=ReadingLabel.D), self.upload(variant="edited")):
            with self.assertRaises(EvaluationInputError):
                self.store.create_snapshot(self.request(reference, first, other))
        with self.assertRaises(ValidationError):
            self.request(reference, first, first)
        d = self.upload(reading=ReadingLabel.D)
        self.assertEqual(self.store.create_snapshot(self.request(reference, d)).reading, ReadingLabel.D)

    def test_failed_publication_never_exposes_partial_snapshot(self):
        reference = self.upload(ground_truth=True)
        hypothesis = self.upload()
        with patch("app.evaluation_inputs.os.rename", side_effect=PermissionError("synthetic-secret")):
            with self.assertRaises(EvaluationInputError) as caught:
                self.store.create_snapshot(self.request(reference, hypothesis))
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_snapshot_tampering_and_filename_traversal_rejected(self):
        reference = self.upload(ground_truth=True)
        hypothesis = self.upload()
        saved = self.store.create_snapshot(self.request(reference, hypothesis))
        path = self.store.root / str(saved.id) / saved.hypotheses[0].file
        path.write_bytes(b"Changed")
        with self.assertRaises(EvaluationInputError):
            self.store.read_snapshot_texts(saved.id)
        second = self.store.create_snapshot(self.request(reference, hypothesis))
        metadata_path = self.store.root / str(second.id) / "metadata.json"
        content = json.loads(metadata_path.read_bytes())
        content["ground_truth"]["file"] = "../../private.txt"
        metadata_path.write_text(json.dumps(content), encoding="utf-8")
        with self.assertRaises(EvaluationInputError):
            self.store.load(second.id)
        imported = self.store.upload("C:\\fakepath\\my.txt", b"text", role="ground_truth")
        self.assertEqual(imported.filename, "my.txt")
        self.assertFalse((self.root / "my.txt").exists())

    def test_managed_unsaved_and_altered_raw_are_not_evaluation_inputs(self):
        run = fixture()
        self.transcripts.checkpoint_run(run)
        attempt = run.readings[0].attempts[0]
        reference = self.upload(ground_truth=True)
        request = EvaluationRequest(ground_truth=UploadedSelection(input_id=reference.id), hypotheses=(
            ManagedSelection(run_id=run.id, attempt_id=attempt.id, variant="raw"),))
        with self.assertRaises(TranscriptStorageError):
            self.store.create_snapshot(request)
        self.transcripts.accept(event(run, "Original"))
        self.transcripts.finalize(run.id, attempt.id, status=Status.COMPLETE)
        self.transcripts.export_path(run.id, attempt.id, "raw").write_bytes(b"Tampered")
        with self.assertRaises(TranscriptStorageError):
            self.store.create_snapshot(request)

    def test_api_upload_selection_snapshot_and_safe_errors(self):
        settings = Settings(self.root / "api")
        with TestClient(create_app(settings)) as client:
            gt = client.post("/api/evaluation-inputs", data={"role": "ground_truth"},
                files={"file": ("LSMzinas_lav_GT.txt", (PROJECT_ROOT / "example" / "LSMzinas_lav_GT.txt").read_bytes())})
            self.assertEqual(gt.status_code, 201)
            hypothesis = client.post("/api/evaluation-inputs",
                data={"role": "hypothesis", "reading": "A", "variant": "raw"},
                files={"file": ("example.txt", b"Example\r\n")})
            self.assertEqual(hypothesis.status_code, 201)
            body = {"ground_truth": {"kind": "upload", "input_id": gt.json()["id"]},
                    "hypotheses": [{"kind": "upload", "input_id": hypothesis.json()["id"]}]}
            response = client.post("/api/evaluations", json=body)
            self.assertEqual(response.status_code, 201)
            saved = response.json()
            self.assertEqual(saved["status"], "inputs_saved")
            self.assertNotIn("scores", saved)
            catalog = client.get("/api/evaluation-inputs").json()
            self.assertEqual(len(catalog), 2)
            self.assertTrue(all(Path(item["disk_path"]).is_file() for item in catalog))
            self.assertEqual(client.get(f'/api/evaluations/{saved["id"]}').json(), saved)
            self.assertEqual(client.get(f"/api/evaluations/{uuid4()}").status_code, 404)
            invalid = client.post("/api/evaluation-inputs", data={"role": "ground_truth"},
                                  files={"file": ("empty.txt", b" \n")})
            self.assertEqual(invalid.status_code, 422)
            malformed = client.post("/api/evaluations", json={"ground_truth": {"input_id": "synthetic-secret"}})
            self.assertEqual(malformed.status_code, 422)
            self.assertNotIn("synthetic-secret", malformed.text)
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get(f'/api/evaluations/{saved["id"]}').json(), saved)
            self.assertEqual(len(client.get("/api/evaluation-inputs").json()), 2)

    def test_api_managed_catalog_uses_saved_reading_and_survives_model_deletion(self):
        with TestClient(create_app(Settings(self.root / "api-managed"))) as client:
            run = RunRecord.model_validate(client.post("/api/runs", json={"mode": "live"}).json())
            reading = fixture().readings[1]
            attempt = reading.attempts[0].evolve(model_entry_id=run.models[0].entry_id)
            run = run.evolve(readings=(reading.evolve(attempts=(attempt,)),))
            store = client.app.state.transcripts
            store.checkpoint_run(run)
            self.assertEqual(client.get(f"/api/runs/{run.id}/evaluation-inputs").json(), [])
            store.accept(event(run, "Pēdējais lasījums."))
            store.finalize(run.id, attempt.id, status=Status.COMPLETE)
            entries = client.get(f"/api/runs/{run.id}/evaluation-inputs").json()
            self.assertEqual(len(entries), 2)
            self.assertEqual({item["selection"]["variant"] for item in entries}, {"raw", "edited"})
            self.assertTrue(all(item["reading"] == "D" and item["directory"] == "Try2" for item in entries))
            gt = client.post("/api/evaluation-inputs", data={"role": "ground_truth"},
                files={"file": ("gt.txt", "Pēdējais lasījums.".encode("utf-8"))}).json()
            response = client.post("/api/evaluations", json={
                "ground_truth": {"kind": "upload", "input_id": gt["id"]},
                "hypotheses": [entries[1]["selection"]]})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["reading"], "D")
            client.delete(f"/api/models/{run.models[0].entry_id}")
            self.assertEqual(client.get(f"/api/runs/{run.id}/evaluation-inputs").json(), entries)


if __name__ == "__main__":
    unittest.main()
