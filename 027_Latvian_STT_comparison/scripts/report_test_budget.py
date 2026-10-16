"""Offline/read-only matrix arithmetic and saved paid-run coverage. No SDKs/env/network."""
import json
import hashlib
import wave
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    plan = json.loads((ROOT/'docs/paid_test_matrix.json').read_text(encoding='utf-8'))
    ledger = json.loads((ROOT/'docs/api_test_usage.json').read_text(encoding='utf-8'))
    dec = lambda value: Decimal(str(value))
    rates = {key:dec(value) for key,value in plan['rates_per_audio_minute'].items()}
    sample = plan['sample']
    path = (ROOT/sample['path']).resolve()
    assert path.is_relative_to(ROOT), 'Sample outside project'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sample['sha256'], 'Sample changed'
    with wave.open(str(path),'rb') as audio:
        assert (audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getnframes()) == (1,2,24000,720000)
    ids = [row['id'] for row in plan['rows']]
    assert len(ids) == len(set(ids)), 'Duplicate matrix ID'
    assert plan['automatic_retries'] == plan['manual_retries_planned'] == 0
    future = Decimal(0)
    for row in plan['rows']:
        assert row['model'] in rates and row['mode'] in ('live','file')
        assert 0 < row['accepted_seconds'] <= 30
        assert row['max_sessions_or_transcriptions'] in (1,2)
        if row.get('status','planned') != 'planned':
            print(f"{row['id']}: already attempted; see usage ledger")
            continue
        cost = dec(row['accepted_seconds'])/60*rates[row['model']]
        future += cost
        print(f"{row['id']}: {row['model']} {row['accepted_seconds']}s total, <= {row['max_sessions_or_transcriptions']} sessions/transcriptions; estimate ${cost:.7f}")
    prior_rows = [row for row in ledger['rows'] if row.get('budget_phase','prior')=='prior']
    new_rows = [row for row in ledger['rows'] if row.get('budget_phase')=='new']
    prior = sum((dec(row['accepted_seconds'])/60*rates[row['model']] for row in prior_rows), Decimal(0))
    observed_new = sum((dec(row['actual_usd']) if row['actual_usd'] is not None else dec(row['accepted_seconds'])/60*rates[row['model']] for row in new_rows),Decimal(0))
    actuals = [row['actual_usd'] for row in prior_rows]
    assert all(value is None or dec(value) >= 0 for value in actuals)
    known_actual = sum((dec(value) for value in actuals if value is not None), Decimal(0))
    reported_total = ledger.get('reported_prior_total_usd')
    accounted_prior = dec(reported_total) if reported_total is not None else known_actual
    assert accounted_prior >= known_actual, 'Reported aggregate is smaller than recorded component bills'
    assert future + observed_new <= dec(plan['new_test_allowance'])
    assert prior <= dec(plan['prior_usage_reserve'])
    assert accounted_prior <= dec(plan['prior_usage_reserve']), 'Prior costs exceed allocation; revise before execution'
    assert sum((dec(plan[key]) for key in ('prior_usage_reserve','new_test_allowance','unallocated_margin')),Decimal(0)) == dec(plan['approved_cap'])
    accounted = {row['run_id'] for row in ledger['rows'] + ledger['out_of_scope_observations']}
    accounted_attempts = {row.get('attempt_id') for row in ledger['rows'] + ledger['out_of_scope_observations']}
    unexpected = []
    for path in (ROOT/'runs').glob('*/file_job.json'):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('execution_mode') == 'paid' and data['run_id'] not in accounted:
            unexpected.append(data['run_id'])
        elif data.get('execution_mode') == 'paid':
            unexpected.extend(f"{data['run_id']}/{identity}" for identity in data['attempts'] if identity not in accounted_attempts)
    for path in (ROOT/'runs').glob('*/Try*/distribution.json'):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('execution_mode') == 'paid' and data['run_id'] not in accounted:
            unexpected.append(data['run_id'])
    print(f'Prior OpenAI/Gemini estimate: ${prior:.7f}; actuals missing: {sum(v is None for v in actuals)}')
    print(f'Pending matrix estimate: ${future:.7f}; historical duration-only baseline plus pending estimate: ${prior+future:.7f}')
    print(f"Pending sessions/transcriptions: {sum(r['max_sessions_or_transcriptions'] for r in plan['rows'] if r.get('status','planned')=='planned')}; automatic retries: 0")
    print(f'Observed new-test cost/estimate: ${observed_new:.7f}; observed plus pending new tests: ${observed_new+future:.7f}')
    if reported_total is not None:
        print(f'User-reported prior cost: ${accounted_prior:.2f}; prior + observed new + planned estimate: ${accounted_prior+observed_new+future:.7f}')
    print(f'Allocation: ${plan["prior_usage_reserve"]} prior + ${plan["new_test_allowance"]} new-test allowance + ${plan["unallocated_margin"]} unallocated = ${plan["approved_cap"]}')
    print(f'Out-of-scope observations: {len(ledger["out_of_scope_observations"])}; unexpected saved paid runs: {unexpected}')
    print(f'Execution ready: {ledger["execution_ready"]}; this report neither authorizes nor starts calls.')
    assert not unexpected, 'Update ledger before further paid work'
    if ledger['execution_ready']:
        assert ledger['unrecorded_usage_reconciled'], 'Confirm current inventory and known additional usage before execution'


if __name__ == '__main__':
    main()
