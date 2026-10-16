"""Disk-backed live history and restart recovery; never reconnect or replay audio."""
import json
from uuid import UUID

from app.contracts import Mode, Status, ErrorCode, ErrorDetails, PartialTranscriptEvent
from app.transcripts import TERMINAL, finalized_text, _write_atomic


INTERRUPTED=ErrorDetails(code=ErrorCode.DISCONNECTED,
    message='Serveris pārstartēts. Saglabāts tikai apstiprinātais teksts; audio netiek atkārtots.')


class LiveHistory:
    def __init__(self, store):
        self.store=store
        self.recovery_errors={}

    def identities(self):
        if not self.store.root.exists():return []
        identities=[]
        for folder in self.store.root.iterdir():
            if not folder.is_dir():continue
            try:identity=UUID(folder.name)
            except ValueError:continue
            try:
                run=self.store.load(identity).run
                if run.mode==Mode.LIVE and run.readings:identities.append((run.created_at,identity))
            except Exception:
                self.recovery_errors[str(identity)]='Izpildes kontrolpunktu nevar nolasīt.'
        return [identity for _,identity in sorted(identities,reverse=True)]

    def recover(self):
        for identity in self.identities():
            try:
                checkpoint=self.store.load(identity)
                states={s.attempt_id:s for s in checkpoint.attempts}
                unfinished=checkpoint.run.status not in TERMINAL
                needs_export=any(a.save_status!='saved' for r in checkpoint.run.readings for a in r.attempts)
                if not unfinished and not needs_export:continue
                for reading in checkpoint.run.readings:
                    for attempt in reading.attempts:
                        state=states.get(attempt.id)
                        if state and state.sealed:
                            if attempt.save_status!='saved':
                                outcome=self.store.finalize(identity,attempt.id,status=attempt.status,errors=attempt.errors)
                                if outcome.save_status!='saved':self.recovery_errors[str(identity)]='Transkripta failu saglabāšana neizdevās.'
                        else:
                            outcome=self.store.finalize(identity,attempt.id,status=Status.INTERRUPTED,
                                errors=(*attempt.errors,INTERRUPTED))
                            if outcome.save_status!='saved':self.recovery_errors[str(identity)]='Transkripta failu saglabāšana neizdevās.'
                current=self.store.load(identity).run
                if unfinished:
                    readings=tuple(r.evolve(status=Status.INTERRUPTED) if r.status not in TERMINAL else r
                        for r in current.readings)
                    self.store.checkpoint_run(current.evolve(status=Status.INTERRUPTED,readings=readings))
                # Never repair/publish a crash-left WAV: header and final bytes may be incomplete.
                _write_atomic(self.store._path(identity,'live_recovery.json'),json.dumps({
                    'interrupted':unfinished,'audio_replayed':False,'capture_reconnected':False,
                    'message':INTERRUPTED.message,'source_audio_requires_inspection':unfinished,
                },ensure_ascii=False).encode('utf-8'))
            except Exception:
                self.recovery_errors[str(identity)]='Pārtrauktās izpildes saglabāšana neizdevās.'

    def view(self, identity):
        checkpoint=self.store.load(identity)
        if checkpoint.run.mode!=Mode.LIVE or not checkpoint.run.readings:raise ValueError('live run')
        states={s.attempt_id:s for s in checkpoint.attempts}
        models={m.entry_id:m for m in checkpoint.run.models}
        items=[]
        for reading in checkpoint.run.readings:
            for attempt in reading.attempts:
                state=states.get(attempt.id)
                items.append({'attempt_id':str(attempt.id),'model':models[attempt.model_entry_id].display_name,
                    'reading':reading.label.value,'status':attempt.status.value,'save_status':attempt.save_status,
                    'errors':[e.model_dump(mode='json') for e in attempt.errors],
                    'save_error':state.save_error.model_dump(mode='json') if state and state.save_error else None,
                    'final_text':finalized_text(state) if state else '',
                    'provisional_text':' '.join(s.text for s in sorted(state.spans,
                        key=lambda s:(s.context.segment_index,s.span_index)) if isinstance(s,PartialTranscriptEvent)) if state else '',
                    'files':{kind:f'/api/runs/{identity}/attempts/{attempt.id}/files/{kind}'
                             for kind in ('raw','editable')} if attempt.save_status=='saved' else {}})
        try:mode=json.loads(self.store._path(identity,'Try1' if checkpoint.run.readings[0].label.value=='A' else 'Try2','distribution.json').read_text(encoding='utf-8'))['execution_mode']
        except (OSError,ValueError,KeyError):mode='demo' if all(m.display_name.endswith(' [DEMO]') for m in checkpoint.run.models) else 'paid'
        return {'run_id':str(identity),'created_at':checkpoint.run.created_at.isoformat(),
            'status':checkpoint.run.status.value,'execution_mode':mode,'attempts':items,
            'recovery_error':self.recovery_errors.get(str(identity))}

    def list(self):
        return {'runs':[{'run_id':str(identity),'created_at':self.store.load(identity).run.created_at.isoformat(),
            'status':self.store.load(identity).run.status.value} for identity in self.identities()[:100]],
            'recovery_errors':self.recovery_errors}
