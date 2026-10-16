"""T026 offline rehearsal: isolated storage, marker PCM and injected fixture text.

No provider SDK, credentials, network or browser operations. Never edits user runs.
"""
import asyncio
from array import array
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import UUID, uuid4
import wave

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.contracts import FinalTranscriptEvent, Mode
from app.registry import ModelRegistry
from app.transcripts import TranscriptStore
from app.live_distribution import LiveDistribution, LiveStart, FrameHeader
from app.live_history import LiveHistory
from app.evaluation_inputs import (EvaluationInputStore, EvaluationRequest, ManagedSelection,
                                   UploadedSelection, EvaluationInputError)
from app.scoring import LegacyScoringStore
from app.diagnostics import DiagnosticStore
from app.results import ResultsStore


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


class FixtureSession:
    def __init__(self, request, text):
        self.request, self.text, self.queue = request, text, asyncio.Queue()

    async def send_audio(self, frame): pass

    async def flush(self):
        # Text is an injected fixture, unrelated to synthetic marker PCM.
        await self.queue.put(FinalTranscriptEvent(context=self.request.context, span_id='fixture',
            span_index=0, revision=1, text=self.text if self.request.context.segment_index == 0 else ''))
        await self.queue.put(None)

    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None: return
            yield event

    async def close(self): pass


async def main():
    root = ROOT/'data'/'rehearsals'/f'T026_{uuid4()}'
    root.mkdir(parents=True, exist_ok=False)
    registry = ModelRegistry(root/'app.sqlite3'); registry.initialize()
    for model in registry.list(Mode.LIVE)[1:]: registry.update(model.id, {'enabled':False})
    store = TranscriptStore(root/'runs')
    fixtures = {'A':ROOT/'example/tests/20260328_ElevenLabs_raw.txt',
                'D':ROOT/'example/tests/20260328_Gemini_Pro31_keyterms.txt'}
    gt_path = ROOT/'example/LSMzinas_lav_GT.txt'
    originals = {path:digest(path) for path in [gt_path,*fixtures.values()]}
    gt = gt_path.read_bytes()
    inputs = EvaluationInputStore(root/'evaluation_inputs',root/'evaluations',store)
    reference = inputs.upload('T026_GroundTruth.txt',gt,role='ground_truth')
    results = ResultsStore(inputs,LegacyScoringStore(inputs),DiagnosticStore(inputs))
    report = {'scope':'OFFLINE ONLY: synthetic PCM, injected fixture text; no speech recognition/browser proof',
              'root':str(root),'checks':{},'readings':[]}
    selections = []
    ui = root/'ui';ui.mkdir()
    (ui/'T026_GroundTruth.txt').write_bytes(gt)

    for label,fixture in fixtures.items():
        text = fixture.read_text(encoding='utf-8-sig').strip()
        class Adapter:
            async def open_live(self, request): return FixtureSession(request,text)
        pipeline = LiveDistribution(registry,store,factory=lambda model:Adapter())
        segment = uuid4()
        await pipeline.start(LiveStart(reading=label,segment_id=segment,sample_rate=8000,channels=1,source_start_sample=0))
        def header(sequence,identity,start,accepted):
            return FrameHeader(sequence=sequence,segment_id=identity,sample_rate=8000,channels=1,frame_samples=80,
                source_start_sample=start,source_end_sample=start+80,audio_start_sample=accepted,audio_end_sample=accepted+80)
        value = 1000 if label=='A' else 2000
        pcm = array('h',[value]*80).tobytes()
        pipeline.offer(header(0,segment,0,0),pcm)
        await pipeline.pause()
        assert pipeline.offer(header(1,segment,80,80),array('h',[3000]*80).tobytes()) is False
        resumed = uuid4()
        await pipeline.resume(LiveStart(reading=label,segment_id=resumed,sample_rate=8000,channels=1,source_start_sample=240))
        pipeline.offer(header(1,resumed,240,80),pcm)
        outcome = await pipeline.stop()
        assert outcome['archive_status']=='saved' and all(s['status']=='complete' for s in outcome['models'].values())
        attempt = store.load(pipeline.run.id).run.readings[0].attempts[0]
        raw = store.export_path(pipeline.run.id,attempt.id,'raw')
        editable = store.export_path(pipeline.run.id,attempt.id,'editable')
        assert raw.read_text(encoding='utf-8') == text and raw.read_bytes()==editable.read_bytes()
        with wave.open(str(raw.parent.parent/'audio.wav'),'rb') as audio:
            samples = array('h'); samples.frombytes(audio.readframes(audio.getnframes()))
            assert len(samples)==160 and list(samples)==[value]*160 and 3000 not in samples
        raw_hash = digest(raw)
        selection = ManagedSelection(run_id=pipeline.run.id,attempt_id=attempt.id,variant='edited')
        selections.append(selection)

        def calculate(variant):
            snapshot = inputs.create_snapshot(EvaluationRequest(ground_truth=UploadedSelection(input_id=reference.id),
                hypotheses=(selection.evolve(variant=variant),)))
            return snapshot,results.calculate(snapshot.id)
        baseline,before = calculate('raw')
        old_exports = {kind:digest(results.export_path(baseline.id,kind)) for kind in ('json','csv')}
        edited_before,unchanged = calculate('edited')
        assert before['legacy']['scores'][0]['wer']['rate']==unchanged['legacy']['scores'][0]['wer']['rate']
        # Correct only an isolated editable copy. User files and fixture originals stay intact.
        editable.write_bytes(gt)
        corrected,after = calculate('edited')
        assert after['legacy']['scores'][0]['wer']['rate']==after['legacy']['scores'][0]['cer']['rate']==0
        assert after['diagnostics']['scores'][0]['diagnostics']['exact_match']
        assert before['legacy']['scores'][0]['wer']['rate']>0 and digest(raw)==raw_hash
        reloaded = ResultsStore(inputs,LegacyScoringStore(inputs),DiagnosticStore(inputs))
        assert reloaded.bundle(baseline.id)['legacy']==before['legacy']
        assert all(digest(reloaded.export_path(baseline.id,kind))==value for kind,value in old_exports.items())
        # Browsers upload copies. Initial edited fixture deliberately equals raw,
        # so the user can repeat the correction through the real Results UI.
        group = ui/('Try1' if label=='A' else 'Try2');group.mkdir()
        (group/'T026_original.txt').write_bytes(raw.read_bytes())
        (group/'T026_editable.txt').write_bytes(raw.read_bytes())
        report['readings'].append({'reading':label,'run_id':str(pipeline.run.id),'directory':group.name,
            'fixture_source':str(fixture),'raw':str(raw),'raw_sha256':raw_hash,'editable_corrected_offline':str(editable),
            'raw_evaluation_id':str(baseline.id),'edited_before_evaluation_id':str(edited_before.id),
            'edited_after_evaluation_id':str(corrected.id),'wer_before':before['legacy']['scores'][0]['wer']['rate'],
            'cer_before':before['legacy']['scores'][0]['cer']['rate'],'wer_after':0,'cer_after':0,
            'synthetic_audio_samples':160,'excluded_marker_absent':True,'source_gap_samples':160})
    try:
        inputs.create_snapshot(EvaluationRequest(ground_truth=UploadedSelection(input_id=reference.id),hypotheses=tuple(selections)))
    except EvaluationInputError:
        report['checks']['mixed_readings_rejected']=True
    else:raise AssertionError('A/D mixing accepted')
    history = LiveHistory(TranscriptStore(root/'runs'));history.recover()
    assert all(history.view(UUID(item['run_id']))['status']=='complete' for item in report['readings'])
    assert all(digest(path)==expected for path,expected in originals.items())
    report['checks'].update(raw_preserved=True,fixture_originals_preserved=True,old_evaluations_immutable=True,
        fresh_disk_edits_change_scores=True,synthetic_pause_exclusion=True,reopened_history=True)
    with (root/'report.json').open('x',encoding='utf-8') as output:json.dump(report,output,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__': asyncio.run(main())
