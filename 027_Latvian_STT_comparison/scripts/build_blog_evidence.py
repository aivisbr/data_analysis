"""Recheck the article's five saved evaluations offline, without changing them."""
import difflib
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.scoring import score_legacy
from app.diagnostics import diagnose

EVALUATIONS = {
    'LT1a': 'c99a43e2-756e-4ac3-a0d0-f72803d8ba96',
    'LT1b': '7de5b56e-7b42-4554-8875-f9ff86b5715b',
    'LT2': 'b1d75067-dd03-4a8c-9be2-68cce835e7a5',
    'AT1': '63b4e467-ad89-457a-a65c-07c4f6b4b944',
    'AT2': '9017a795-045e-43ef-82b4-8053cb507115',
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    attempts = {}
    for path in (ROOT/'runs').glob('*/metadata.json'):
        checkpoint = read(path)
        run = checkpoint['run']
        models = {m['entry_id']: m for m in run['models']}
        for reading in run['readings']:
            for attempt in reading['attempts']:
                state = next(s for s in checkpoint['attempts'] if s['attempt_id'] == attempt['id'])
                attempts[attempt['id']] = (path.parent, run, reading, attempt, state, models[attempt['model_entry_id']])
    report = {'purpose': 'Article evidence; no provider calls or evaluation writes.',
              'at2_reading': 'D per user; source evaluation/run metadata records A.', 'evaluations': {}}
    hashes = set()
    unique = set()
    for test, identity in EVALUATIONS.items():
        folder = ROOT/'evaluations'/identity
        meta = read(folder/'metadata.json')
        legacy = read(folder/'legacy_scores.json')
        diagnostic = read(folder/'diagnostics.json')
        assert legacy['snapshot'] == diagnostic['snapshot'] == meta
        for item in [meta['ground_truth'], *meta['hypotheses']]:
            payload = (folder/item['file']).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == item['sha256']
        reference = (folder/meta['ground_truth']['file']).read_text(encoding='utf-8')
        hashes.add(meta['ground_truth']['sha256'])
        result = {'id': identity, 'metadata_reading': meta['reading'], 'ground_truth_sha256': meta['ground_truth']['sha256'], 'rows': []}
        assert len(meta['hypotheses']) == len(legacy['scores']) == len(diagnostic['scores'])
        for i, (item, saved, saved_d) in enumerate(zip(meta['hypotheses'], legacy['scores'], diagnostic['scores'])):
            assert saved['input_index'] == saved_d['input_index'] == i
            assert saved['input_sha256'] == saved_d['input_sha256'] == item['sha256']
            text = (folder/item['file']).read_text(encoding='utf-8')
            wer, cer = score_legacy(reference, text)
            diagnostics = diagnose(reference, text)
            assert wer.model_dump() == saved['wer'] and cer.model_dump() == saved['cer']
            assert diagnostics.model_dump(mode='json') == saved_d['diagnostics']
            attempt_id = re.findall(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', item['label'])[-1]
            run_dir, run, reading, attempt, state, model = attempts[attempt_id]
            original = ' '.join(s['text'].strip() for s in sorted(state['spans'], key=lambda s: (s['context']['segment_index'], s['span_index']))
                                if s['type'] == 'transcript.final' and s['text'].strip())
            assert hashlib.sha256(original.encode()).hexdigest() == state['raw_sha256']
            changes = [{'operation': tag, 'original': original[a:b], 'evaluated': text[c:d]}
                       for tag, a, b, c, d in difflib.SequenceMatcher(None, original, text, autojunk=False).get_opcodes() if tag != 'equal']
            row = {'label': item['label'], 'model': model['model_id'], 'run_id': run['id'], 'attempt_id': attempt_id,
                   'input_sha256': item['sha256'], 'effective_settings': model['effective_settings'],
                   'processing_status': attempt['status'], 'checkpoint_to_evaluated_changes': changes,
                   'wer': wer.model_dump(), 'cer': cer.model_dump(),
                   'strict_cer': diagnostics.strict_cer.model_dump(),
                   'punctuation': diagnostics.punctuation.totals.model_dump(),
                   'capitalization': diagnostics.capitalization.model_dump(), 'exact_match': diagnostics.exact_match}
            if run['mode'] == 'live':
                source = read(next(run_dir.glob('Try*/distribution.json')))
                row['audio_seconds'] = source['accepted_samples']/source['source']['sample_rate']
                row['source_segments'] = source['segment_index']+1
            else:
                source = read(run_dir/'file_job.json')['preparation']
                row['audio_seconds'] = source['conversion']['actual_duration_seconds']
                row['prepared_audio_sha256'] = source['conversion']['interval_sha256']
            result['rows'].append(row)
            unique.add(item['sha256'])
        report['evaluations'][test] = result
    assert len(hashes) == 1
    report['checks'] = {'hashes_and_fresh_scores_match': True, 'same_ground_truth': True,
                        'evaluation_rows': sum(len(e['rows']) for e in report['evaluations'].values()),
                        'unique_transcripts': len(unique)}
    output = ROOT/'output/articles/diktats_2026_evidence.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report['checks']))
    print(output)


if __name__ == '__main__':
    main()
