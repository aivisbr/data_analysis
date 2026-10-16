"""Offline persistence, event ordering, recovery and disk-failure checks."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import (
    AttemptRecord, CaptureSegment, ErrorCode, ErrorDetails, ErrorEvent, EventContext,
    FinalTranscriptEvent, Mode, ModelEntry, ModelSnapshot, PartialTranscriptEvent,
    Provider, ReadingLabel, ReadingRecord, RunRecord, Status,
)
from app.main import create_app
from app.transcripts import TranscriptStore, TranscriptStorageError


def fixture():
    model = ModelEntry(provider=Provider.OPENAI, model_id="gpt-transcribe", mode=Mode.LIVE,
                       display_name="Latviešu pārbaude", capability_profile_id="openai:gpt-transcribe:live")
    readings = []
    for label in (ReadingLabel.A, ReadingLabel.D):
        first = CaptureSegment(index=0, source_start_ms=0, source_end_ms=100,
                               audio_start_ms=0, audio_end_ms=100)
        second = CaptureSegment(index=1, source_start_ms=500, source_end_ms=600,
                                audio_start_ms=100, audio_end_ms=200)
        attempt = AttemptRecord(model_entry_id=model.id, segment_ids=(first.id, second.id))
        readings.append(ReadingRecord(label=label, segments=(first, second), attempts=(attempt,)))
    return RunRecord(mode=Mode.LIVE, models=(ModelSnapshot.from_entry(model),), readings=tuple(readings))


def event(run, text, *, reading=0, segment=0, span=0, span_id=None, revision=1, partial=False):
    record = run.readings[reading]
    context = EventContext(run_id=run.id, reading=record.label,
        attempt_id=record.attempts[-1].id, model_entry_id=record.attempts[-1].model_entry_id,
        segment_id=record.segments[segment].id, segment_index=segment)
    cls = PartialTranscriptEvent if partial else FinalTranscriptEvent
    return cls(context=context, text=text, span_id=span_id or str(span), span_index=span, revision=revision)


class TranscriptTests(unittest.TestCase):
    def test_atomic_checkpoint_retries_windows_sharing_failure_only(self):
        from app.transcripts import _write_atomic
        import os
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'metadata.json';path.write_bytes(b'old')
            denied=PermissionError('sharing');denied.winerror=5
            original=os.replace
            with patch('app.transcripts.os.replace',side_effect=[denied,denied,None]) as replace, patch('app.transcripts.time.sleep') as sleep:
                # Execute the real replacement only on the successful attempt.
                def attempt(source,target):
                    if replace.call_count<3:raise denied
                    return original(source,target)
                replace.side_effect=attempt
                _write_atomic(path,b'new')
                self.assertEqual(replace.call_count,3);self.assertEqual(sleep.call_count,2)
            self.assertEqual(path.read_bytes(),b'new')
            with patch('app.transcripts.os.replace',side_effect=denied) as replace, patch('app.transcripts.time.sleep'):
                with self.assertRaises(PermissionError):_write_atomic(path,b'never')
                self.assertEqual(replace.call_count,21)
            self.assertEqual(path.read_bytes(),b'new')
            self.assertEqual(list(Path(folder).glob('.pending-*')),[])
            with patch('app.transcripts.os.replace',side_effect=OSError('disk full')) as replace:
                with self.assertRaises(OSError):_write_atomic(path,b'never')
                self.assertEqual(replace.call_count,1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runs"
        self.store = TranscriptStore(self.root)
        self.run = fixture()
        self.store.checkpoint_run(self.run)

    def finish(self, reading=0, **kwargs):
        return self.store.finalize(self.run.id, self.run.readings[reading].attempts[-1].id,
                                   status=kwargs.pop("status", Status.COMPLETE), **kwargs)

    def test_order_revisions_duplicates_and_no_provisional_in_exports(self):
        last = event(self.run, " beigas.", segment=1)
        self.assertTrue(self.store.accept(last))
        self.assertFalse(self.store.accept(last))
        self.store.accept(event(self.run, "MELNRAKSTS", span=2, partial=True))
        partial = event(self.run, "Rigs", partial=True)
        self.store.accept(partial)
        self.store.accept(event(self.run, "Rīga,", revision=2))
        self.assertFalse(self.store.accept(event(self.run, "VECĀKS", revision=1)))
        self.assertFalse(self.store.accept(event(self.run, "NEDRĪKST", revision=99, partial=True)))
        self.store.accept(event(self.run, "Latvija!", span=1))
        saved = self.finish()
        raw = self.store.export_path(self.run.id, saved.id, "raw").read_bytes()
        self.assertEqual(raw, "Rīga, Latvija! beigas.".encode("utf-8"))
        self.assertEqual(saved.save_status, "saved")
        metadata = self.store.load(self.run.id)
        self.assertIn("MELNRAKSTS", metadata.model_dump_json())
        self.assertEqual(metadata.attempts[0].raw_sha256, sha256(raw).hexdigest())
        self.assertFalse(self.store.accept(event(self.run, "par vēlu", span=3)))

    def test_try1_try2_separate_even_when_d_finishes_first(self):
        self.store.accept(event(self.run, "Pēdējais lasījums.", reading=1))
        d = self.finish(1)
        self.store.accept(event(self.run, "Pirmais lasījums."))
        a = self.finish()
        self.assertTrue(a.raw_file.startswith("Try1/raw/"))
        self.assertTrue(d.raw_file.startswith("Try2/raw/"))
        self.assertEqual(self.store.export_path(self.run.id, d.id, "raw").read_text(encoding="utf-8"),
                         "Pēdējais lasījums.")
        self.assertFalse((self.root / str(self.run.id) / "A").exists())
        self.assertFalse((self.root / str(self.run.id) / "D").exists())

    def test_restart_recovers_checkpoint_and_deduplicates(self):
        original = event(self.run, "Ābols.")
        self.store.accept(original)
        self.store = TranscriptStore(self.root)
        self.assertFalse(self.store.accept(original))
        self.store.accept(event(self.run, "Ābele.", revision=2))
        saved = self.finish()
        self.assertEqual(self.store.export_path(self.run.id, saved.id, "raw").read_bytes(),
                         "Ābele.".encode("utf-8"))

    def test_editable_copy_and_raw_preserved_on_repeated_finalize_and_retry(self):
        self.store.accept(event(self.run, "Sākums."))
        saved = self.finish()
        edited = self.store.export_path(self.run.id, saved.id, "editable")
        edited.write_text("Mani labojumi!", encoding="utf-8")
        self.store = TranscriptStore(self.root)
        self.finish()
        self.assertEqual(edited.read_text(encoding="utf-8"), "Mani labojumi!")
        progress = self.store.load(self.run.id).run
        retry = AttemptRecord(model_entry_id=saved.model_entry_id, number=2,
                              segment_ids=saved.segment_ids)
        reading = progress.readings[0].evolve(attempts=(*progress.readings[0].attempts, retry))
        self.run = progress.evolve(readings=(reading, progress.readings[1]))
        self.store.checkpoint_run(self.run)
        self.store.accept(event(self.run, "Atkārtots mēģinājums."))
        second = self.finish()
        self.assertNotEqual(saved.raw_file, second.raw_file)
        self.assertEqual(edited.read_text(encoding="utf-8"), "Mani labojumi!")
        self.assertEqual(self.store.export_path(self.run.id, saved.id, "raw").read_text(encoding="utf-8"),
                         "Sākums.")

    def test_empty_failed_text_and_safe_errors_only_in_metadata(self):
        error = ErrorDetails(code=ErrorCode.TIMEOUT, message="Pārsniegts gaidīšanas laiks.")
        self.store.accept(ErrorEvent(context=event(self.run, "").context, error=error))
        saved = self.finish(status=Status.FAILED, errors=(error,))
        self.assertEqual(saved.status, Status.FAILED)
        self.assertEqual(saved.save_status, "saved")
        self.assertEqual(self.store.export_path(self.run.id, saved.id, "raw").read_bytes(), b"")
        self.assertIn(error.message, self.store.load(self.run.id).model_dump_json())

    def test_partial_output_survives_interruption_without_promoting_partial(self):
        self.store.accept(event(self.run, "Saglabāta runa."))
        self.store.accept(event(self.run, "Vēl nepabeigts", span=1, partial=True))
        error = ErrorDetails(code=ErrorCode.DISCONNECTED, message="Savienojums pārtrūka.")
        saved = self.finish(status=Status.INTERRUPTED, errors=(error,))
        self.assertEqual(self.store.export_path(self.run.id, saved.id, "raw").read_text(encoding="utf-8"),
                         "Saglabāta runa.")

    def test_export_failure_recorded_and_recoverable_without_overwriting_edits(self):
        self.store.accept(event(self.run, "Teksts."))
        from app.transcripts import _write_atomic
        def fail_editable(path, payload, **kwargs):
            if path.parent.name == "editable":
                raise PermissionError("synthetic-secret-path")
            return _write_atomic(path, payload, **kwargs)
        with patch("app.transcripts._write_atomic", side_effect=fail_editable):
            failed = self.finish()
        self.assertEqual(failed.save_status, "failed")
        state = self.store.load(self.run.id).attempts[0]
        self.assertEqual(state.save_error.code, ErrorCode.STORAGE)
        self.assertNotIn("synthetic-secret", state.model_dump_json())
        self.assertIsNone(failed.editable_file)
        self.store = TranscriptStore(self.root)
        saved = self.finish()
        self.assertEqual(saved.save_status, "saved")
        self.assertIsNone(self.store.load(self.run.id).attempts[0].save_error)

    def test_failed_atomic_checkpoint_leaves_previous_valid_json(self):
        original = (self.root / str(self.run.id) / "metadata.json").read_bytes()
        with patch("app.transcripts.os.replace", side_effect=PermissionError("secret")):
            with self.assertRaisesRegex(TranscriptStorageError, "saglabāt diskā"):
                self.store.accept(event(self.run, "Nebūs saglabāts"))
        self.assertEqual((self.root / str(self.run.id) / "metadata.json").read_bytes(), original)
        self.assertEqual(list(self.root.rglob(".pending-*")), [])
        self.assertEqual(self.store.load(self.run.id).attempts, ())

    def test_raw_tampering_is_not_overwritten_or_downloaded(self):
        self.store.accept(event(self.run, "Oriģināls."))
        saved = self.finish()
        raw = self.store.export_path(self.run.id, saved.id, "raw")
        raw.write_text("Izmainīts", encoding="utf-8")
        with self.assertRaises(TranscriptStorageError):
            self.store.export_path(self.run.id, saved.id, "raw")
        failed = self.finish()
        self.assertEqual(failed.save_status, "failed")
        self.assertEqual(raw.read_text(encoding="utf-8"), "Izmainīts")

    def test_failure_after_text_publication_does_not_claim_saved(self):
        self.store.accept(event(self.run, "Noslēgts teksts."))
        original_save = self.store._save
        count = 0
        def fail_second(checkpoint):
            nonlocal count
            count += 1
            if count == 2:
                raise TranscriptStorageError()
            return original_save(checkpoint)
        with patch.object(self.store, "_save", side_effect=fail_second):
            with self.assertRaises(TranscriptStorageError):
                self.finish()
        old = self.store.load(self.run.id)
        self.assertTrue(old.attempts[0].sealed)
        self.assertEqual(old.run.readings[0].attempts[0].save_status, "pending")
        files = list((self.root / str(self.run.id)).rglob("*.txt"))
        self.assertEqual(len(files), 2)
        edited = next(p for p in files if p.parent.name == "editable")
        edited.write_text("Labojums pēc pārtraukuma.", encoding="utf-8")
        self.store = TranscriptStore(self.root)
        self.assertEqual(self.finish().save_status, "saved")
        self.assertEqual(edited.read_text(encoding="utf-8"), "Labojums pēc pārtraukuma.")

    def test_valid_json_with_corrupted_event_identity_is_rejected_safely(self):
        self.store.accept(event(self.run, "Teksts."))
        metadata = self.root / str(self.run.id) / "metadata.json"
        content = json.loads(metadata.read_text(encoding="utf-8"))
        content["attempts"][0]["spans"][0]["context"]["reading"] = "D"
        metadata.write_text(json.dumps(content), encoding="utf-8")
        with self.assertRaises(TranscriptStorageError):
            self.store.load(self.run.id)

    def test_exclusive_publish_collision_preserves_existing_file(self):
        from app.transcripts import _write_atomic
        path = self.root / "collision.txt"
        _write_atomic(path, "Pirmais.".encode("utf-8"), exclusive=True)
        with self.assertRaises(FileExistsError):
            _write_atomic(path, b"overwrite", exclusive=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "Pirmais.")
        self.assertEqual(list(self.root.rglob(".pending-*")), [])

    def test_conflicting_span_identity_or_event_context_rejected(self):
        self.store.accept(event(self.run, "Sākums"))
        with self.assertRaises(ValueError):
            self.store.accept(event(self.run, "Cits fragments", span_id="other"))
        with self.assertRaises(ValueError):
            self.store.accept(event(self.run, "Pārvietots", span=1, span_id="0", revision=2))
        bad = event(self.run, "Nepareizs")
        with self.assertRaises(ValueError):
            self.store.accept(bad.evolve(context=bad.context.evolve(reading=ReadingLabel.D)))

    def test_history_and_save_claims_cannot_be_rewritten_by_checkpoint(self):
        before = self.run.readings[0].attempts[0]
        fake = before.evolve(save_status="saved", raw_file="fake.txt", editable_file="fake.txt")
        with self.assertRaises(ValueError):
            self.store.checkpoint_run(self.run.evolve(readings=(
                self.run.readings[0].evolve(attempts=(fake,)), self.run.readings[1])))
        self.finish()
        with self.assertRaises(ValueError):
            self.store.checkpoint_run(self.run)
        with self.assertRaises(ValueError):
            self.store.checkpoint_run(self.store.load(self.run.id).run.evolve(readings=()))

    def test_file_failure_isolated_from_another_model(self):
        second = self.run.models[0].evolve(entry_id=uuid4(), display_name="Otrais")
        attempt = AttemptRecord(model_entry_id=second.entry_id,
                                segment_ids=self.run.readings[0].attempts[0].segment_ids)
        reading = self.run.readings[0].evolve(attempts=(*self.run.readings[0].attempts, attempt))
        # New model selection must be frozen from the outset, so use a new run.
        self.run = self.run.evolve(id=uuid4(), models=(*self.run.models, second),
                                   readings=(reading, self.run.readings[1]))
        self.store.checkpoint_run(self.run)
        first_id = self.run.readings[0].attempts[0].id
        with patch("app.transcripts.os.link", side_effect=PermissionError("synthetic")):
            first = self.store.finalize(self.run.id, first_id, status=Status.COMPLETE)
        self.assertEqual(first.save_status, "failed")
        self.store.accept(event(self.run, "Otrais turpināja."))
        second_result = self.finish()
        self.assertEqual(second_result.save_status, "saved")

    def test_api_checkpoint_and_registered_downloads(self):
        with TestClient(create_app(Settings(Path(self.temp.name) / "api"))) as client:
            run = RunRecord.model_validate(client.post("/api/runs", json={"mode": "live"}).json())
            segment = CaptureSegment(index=0, source_start_ms=0, source_end_ms=10,
                                     audio_start_ms=0, audio_end_ms=10)
            attempt = AttemptRecord(model_entry_id=run.models[0].entry_id, segment_ids=(segment.id,))
            run = run.evolve(readings=(ReadingRecord(label=ReadingLabel.A, segments=(segment,),
                                                     attempts=(attempt,)),))
            store = client.app.state.transcripts
            store.checkpoint_run(run)
            store.accept(event(run, "Latvija — mūsu mājas."))
            saved = store.finalize(run.id, attempt.id, status=Status.COMPLETE)
            updated = client.get(f"/api/runs/{run.id}").json()
            self.assertEqual(updated["readings"][0]["attempts"][0]["save_status"], "saved")
            route = f"/api/runs/{run.id}/attempts/{attempt.id}/files/"
            response = client.get(route + "raw")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, "Latvija — mūsu mājas.".encode("utf-8"))
            self.assertIn("attachment", response.headers["content-disposition"])
            store.export_path(run.id, saved.id, "editable").write_text("Labots.", encoding="utf-8")
            self.assertEqual(client.get(route + "editable").text, "Labots.")
            self.assertEqual(client.get(route + "metadata.json").status_code, 422)
            self.assertEqual(client.get(f"/api/runs/{run.id}/attempts/{uuid4()}/files/raw").status_code, 404)
            client.delete(f"/api/models/{run.models[0].entry_id}")
            self.assertEqual(client.get(route + "raw").status_code, 200)


if __name__ == "__main__":
    unittest.main()
