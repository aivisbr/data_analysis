"""Meaningful record/link validation; no providers or credentials required."""

import unittest
from datetime import datetime
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.contracts import (
    AttemptRecord, CaptureSegment, ErrorCode, ErrorDetails, ErrorEvent,
    EventContext, FinalTranscriptEvent, Mode, ModelEntry, ModelSnapshot,
    PartialTranscriptEvent, Provider, ProviderEvent, ReadingLabel, ReadingRecord,
    RunRecord, Status, StatusEvent, TranscriptionSettings,
)
from app.providers.base import AudioFrame, FileRequest, LiveRequest, LiveSession, ProviderAdapter
from app.providers.capabilities import CAPABILITY_PROFILES, Support, get_profile


def entry(mode=Mode.LIVE):
    profile = next(p for p in CAPABILITY_PROFILES if p.mode == mode)
    return ModelEntry(provider=profile.provider, model_id=profile.model_id,
                      display_name="Paraugs", mode=mode, capability_profile_id=profile.id)


def fixture():
    model = entry()
    segment = CaptureSegment(index=0, source_start_ms=0, source_end_ms=1000,
                             audio_start_ms=0, audio_end_ms=1000)
    attempt = AttemptRecord(model_entry_id=model.id, segment_ids=(segment.id,))
    reading = ReadingRecord(label=ReadingLabel.A, segments=(segment,), attempts=(attempt,))
    run = RunRecord(mode=Mode.LIVE, models=(ModelSnapshot.from_entry(model),), readings=(reading,))
    context = EventContext(run_id=run.id, reading=ReadingLabel.A, attempt_id=attempt.id,
                           model_entry_id=model.id, segment_id=segment.id, segment_index=0)
    return run, context


class ContractTests(unittest.TestCase):
    def test_snapshot_is_immutable_and_independent_of_registry_edits(self):
        original = entry()
        snapshot = ModelSnapshot.from_entry(original)
        edited = original.evolve(settings=TranscriptionSettings(language_hint="en", keywords=("new",)))
        self.assertEqual(snapshot.requested_settings.language_hint, "lv-LV")
        self.assertEqual(edited.settings.language_hint, "en")
        with self.assertRaises(ValidationError):
            snapshot.effective_settings.language_hint = "en"
        with self.assertRaises(ValueError):
            ModelSnapshot.from_entry(original.evolve(enabled=False))

    def test_settings_reject_provider_fields_and_invalid_input(self):
        for payload in ({"api_key": "synthetic"}, {"language_hint": ""},
                        {"keywords": ["\n"]}, {"latency": "instant"}):
            with self.assertRaises(ValidationError):
                TranscriptionSettings.model_validate(payload)
        self.assertIsNone(TranscriptionSettings(language_hint=None).language_hint)

    def test_run_roundtrip_preserves_ids_and_utc(self):
        run, _ = fixture()
        self.assertEqual(RunRecord.model_validate_json(run.model_dump_json()), run)
        with self.assertRaises(ValidationError):
            run.evolve(created_at=datetime(2026, 10, 14))
        with self.assertRaises(ValidationError):
            run.evolve(mode=Mode.FILE)

    def test_paused_time_does_not_become_accepted_audio(self):
        run, _ = fixture()
        reading = run.readings[0]
        segment = CaptureSegment(index=1, source_start_ms=10000, source_end_ms=11000,
                                 audio_start_ms=1000, audio_end_ms=2000)
        updated = reading.evolve(segments=(*reading.segments, segment))
        self.assertEqual(updated.segments[-1].audio_end_ms, 2000)
        for invalid in (segment.evolve(index=2), segment.evolve(audio_start_ms=1500),
                        segment.evolve(source_start_ms=500)):
            with self.assertRaises(ValidationError):
                reading.evolve(segments=(*reading.segments, invalid))

    def test_a_d_cannot_share_segments_or_attempts(self):
        run, _ = fixture()
        a = run.readings[0]
        d = a.evolve(label=ReadingLabel.D)
        with self.assertRaises(ValidationError):
            run.evolve(readings=(a, d))
        with self.assertRaises(ValidationError):
            run.evolve(readings=(a, a))
        with self.assertRaises(ValidationError):
            a.evolve(attempts=(a.attempts[0].evolve(segment_ids=(uuid4(),)),))

    def test_incomplete_output_cannot_claim_complete_or_saved(self):
        run, _ = fixture()
        attempt = run.readings[0].attempts[0]
        error = ErrorDetails(code=ErrorCode.TIMEOUT, message="Izpilde nav pabeigta.")
        incomplete = attempt.evolve(status=Status.INCOMPLETE, errors=(error,))
        self.assertEqual(incomplete.save_status, "pending")
        with self.assertRaises(ValidationError):
            incomplete.evolve(status=Status.COMPLETE)
        with self.assertRaises(ValidationError):
            attempt.evolve(status=Status.FAILED)
        with self.assertRaises(ValidationError):
            attempt.evolve(save_status="saved")
        with self.assertRaises(ValidationError):
            run.evolve(status=Status.COMPLETE)

    def test_event_revisions_and_context_are_explicit(self):
        run, context = fixture()
        partial = PartialTranscriptEvent(context=context, span_id="turn-1", span_index=0,
                                         revision=1, text="Pirm")
        final = FinalTranscriptEvent(context=context, span_id="turn-1", span_index=0,
                                     revision=2, text="Pirmais.")
        adapter = TypeAdapter(ProviderEvent)
        for event in (partial, final,
                      ErrorEvent(context=context, error=ErrorDetails(code=ErrorCode.DISCONNECTED,
                                                                    message="Savienojums pārtrūka.")),
                      StatusEvent(context=context, status=Status.FINALIZING)):
            restored = adapter.validate_json(event.model_dump_json())
            restored.context.validate_for_run(run)
            self.assertEqual(restored, event)
        self.assertEqual(partial.span_id, final.span_id)
        with self.assertRaises(ValueError):
            context.evolve(reading=ReadingLabel.D).validate_for_run(run)
        with self.assertRaises(ValueError):
            context.evolve(segment_index=2).validate_for_run(run)
        with self.assertRaises(ValidationError):
            partial.evolve(revision=0)

    def test_successful_retry_keeps_prior_failure_without_hiding_missing_models(self):
        run, _ = fixture()
        attempt = run.readings[0].attempts[0]
        failed = attempt.evolve(status=Status.FAILED, errors=(
            ErrorDetails(code=ErrorCode.TIMEOUT, message="Iepriekšējais mēģinājums neizdevās."),))
        retry = attempt.evolve(id=uuid4(), number=2, status=Status.COMPLETE)
        reading = run.readings[0].evolve(status=Status.COMPLETE, attempts=(failed, retry))
        finished = run.evolve(readings=(reading,), status=Status.COMPLETE)
        self.assertEqual(finished.readings[0].attempts[0].status, Status.FAILED)
        with self.assertRaises(ValidationError):
            finished.evolve(models=(*run.models, ModelSnapshot.from_entry(entry())))

    def test_frames_validate_pcm_boundaries(self):
        frame = AudioFrame(uuid4(), 0, 0, 16000, b"\x00\x00")
        self.assertEqual(len(frame.pcm), 2)
        with self.assertRaises(ValueError):
            AudioFrame(frame.segment_id, 0, 0, 16000, b"\x00")

    def test_requests_reject_wrong_model_and_mode(self):
        run, context = fixture()
        request = LiveRequest(context=context, model=run.models[0])
        self.assertEqual(request.model.entry_id, context.model_entry_id)
        with self.assertRaises(ValidationError):
            LiveRequest(context=context.evolve(model_entry_id=uuid4()), model=run.models[0])
        with self.assertRaises(ValidationError):
            FileRequest(context=context, model=run.models[0], audio_path="sample.wav", mime_type="audio/wav")

    def test_retained_roster_and_unverified_capabilities(self):
        roster = {(p.provider.value, p.model_id, p.mode.value) for p in CAPABILITY_PROFILES}
        self.assertEqual(roster, {
            ("openai", "gpt-transcribe", "live"), ("openai", "gpt-transcribe", "file"),
            ("openai", "gpt-live-transcribe", "live"), ("openai", "gpt-realtime-whisper", "live"),
            ("gemini", "gemini-3.5-transcribe-live", "live"), ("gemini", "gemini-3.5-transcribe", "file"),
            ("elevenlabs", "scribe_v2_realtime", "live"), ("elevenlabs", "scribe_v2", "file"),
        })
        live = get_profile("openai:gpt-live-transcribe:live")
        self.assertEqual(live.latency, Support.UNVERIFIED)
        whisper = get_profile("openai:gpt-realtime-whisper:live")
        self.assertEqual(whisper.prompt, Support.UNSUPPORTED)
        self.assertEqual(whisper.automatic_turn_detection, Support.UNSUPPORTED)

    def test_protocol_can_be_implemented_without_sdk(self):
        class Session:
            async def send_audio(self, frame): pass
            async def flush(self): pass
            async def close(self): pass
            async def _events(self):
                if False: yield
            def events(self): return self._events()

        class Adapter:
            async def transcribe_file(self, request): pass
            async def open_live(self, request): return Session()

        self.assertIsInstance(Session(), LiveSession)
        self.assertIsInstance(Adapter(), ProviderAdapter)


if __name__ == "__main__":
    unittest.main()
