"""Single-process, independently scheduled file jobs with durable attempt provenance."""
import asyncio
import json
import threading
from uuid import UUID

from app.contracts import (AttemptRecord, CaptureSegment, ErrorCode, ErrorDetails, EventContext,
    FinalTranscriptEvent, Mode, Provider, ReadingRecord, Status)
from app.providers.base import FileRequest, FileTranscript, ProviderFailure
from app.providers.prepared_audio import read_prepared_audio
from app.registry import RegistryError
from app.transcripts import _write_atomic, finalized_text

TERMINAL = {Status.COMPLETE, Status.FAILED, Status.INCOMPLETE, Status.INTERRUPTED, Status.CANCELLED}


class DemoAdapter:
    def __init__(self, provider, number):
        self.provider, self.number = provider, number

    async def transcribe_file(self, request):
        await asyncio.to_thread(read_prepared_audio, request)
        await asyncio.sleep(.3)
        if self.provider == Provider.GEMINI and self.number == 1:
            raise ProviderFailure(ErrorDetails(code=ErrorCode.UNAVAILABLE_MODEL,
                message='DEMO: simulēta modeļa kļūda; citi modeļi turpina darbu.'))
        return FileTranscript(text='Demonstrācijas pieraksts. Tas nav audio transkripts.')


def adapter_for(provider):
    if provider == Provider.MISTRAL:
        from app.providers.mistral_file import MistralFileAdapter
        return MistralFileAdapter()
    if provider == Provider.OPENAI:
        from app.providers.openai_file import OpenAIFileAdapter
        return OpenAIFileAdapter()
    if provider == Provider.GEMINI:
        from app.providers.gemini_file import GeminiFileAdapter
        return GeminiFileAdapter()
    from app.providers.elevenlabs_file import ElevenLabsFileAdapter
    return ElevenLabsFileAdapter()


class FileJobs:
    def __init__(self, registry, transcripts, audio, *, paid_enabled=False, factory=None, chunk_timeout=200):
        self.registry, self.transcripts, self.audio = registry, transcripts, audio
        self.paid_enabled, self.factory, self.chunk_timeout = paid_enabled, factory, chunk_timeout
        self.tasks = {}
        self.storage_failures = {}
        self.lock = threading.RLock()

    def _path(self, identity):
        return self.transcripts._path(UUID(str(identity)), 'file_job.json')

    def _load(self, identity):
        try:
            return json.loads(self._path(identity).read_text('utf-8'))
        except FileNotFoundError:
            raise RegistryError('not_found', 'Faila izpilde nav atrasta.') from None
        except (OSError, ValueError):
            raise RegistryError('storage', 'Faila izpildi nevar nolasīt.') from None

    def _save(self, identity, data):
        try:
            _write_atomic(self._path(identity), json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
        except OSError:
            raise RegistryError('storage', 'Faila izpildes metadatus nevar saglabāt.') from None

    def _check_budget(self, mode):
        if mode == 'paid' and not self.paid_enabled:
            raise RegistryError('conflict', 'Maksas API izpilde ir bloķēta, kamēr nav saskaņots budžets. Izmanto DEMO pārbaudi.')

    def _new(self, preparation_id, mode):
        self._check_budget(mode)
        _, preparation = self.audio._load('prepared', preparation_id)
        # Verify the common interval/chunks before creating any provider task.
        self.audio.prepared_path(preparation_id)
        for chunk in preparation['chunks']:
            self.audio.prepared_path(preparation_id, chunk['index'])
        enabled = [m for m in self.registry.list(Mode.FILE) if m.enabled]
        if any(m.provider == Provider.ELEVENLABS for m in enabled) and any(
                c['end_sample'] - c['start_sample'] < 2400 for c in preparation['chunks']):
            raise RegistryError('invalid', 'ElevenLabs nepieciešami vismaz 100 ms katrā fragmentā. Sagatavo citu kopīgo intervālu; īsā aste netiks atmesta.')
        run = self.registry.create_run(Mode.FILE, demo=mode == 'demo')
        start = round(preparation['source_interval']['start_seconds'] * 1000)
        segments = tuple(CaptureSegment(index=c['index'], source_start_ms=start + round(c['start_sample'] / 24),
            source_end_ms=start + round(c['end_sample'] / 24), audio_start_ms=round(c['start_sample'] / 24),
            audio_end_ms=round(c['end_sample'] / 24)) for c in preparation['chunks'])
        attempts = tuple(AttemptRecord(model_entry_id=m.entry_id, segment_ids=tuple(s.id for s in segments)) for m in run.models)
        run = run.evolve(status=Status.RUNNING, readings=(ReadingRecord(label=preparation['reading'],
            status=Status.RUNNING, segments=segments, attempts=attempts),))
        self.transcripts.checkpoint_run(run)
        data = dict(schema_version=1, run_id=str(run.id), preparation_id=str(preparation_id),
            preparation=preparation,
            execution_mode=mode, attempts={str(a.id): dict(completed_chunks=0, results=[], storage_error=None) for a in attempts})
        self._save(run.id, data)
        return run.id, attempts

    async def start(self, preparation_id, mode):
        identity, attempts = await asyncio.to_thread(self._new, preparation_id, mode)
        for attempt in attempts:
            self._schedule(identity, attempt.id)
        return await asyncio.to_thread(self.view, identity)

    def _schedule(self, identity, attempt_id):
        key = (identity, attempt_id)
        task = asyncio.create_task(self._work(identity, attempt_id))
        self.tasks[key] = task
        def finished(done):
            self.tasks.pop(key, None)
            if not done.cancelled() and done.exception() is not None:
                self.storage_failures[key] = 'Izpildes noslēgšanu nevar saglabāt.'
        task.add_done_callback(finished)

    def _state(self, identity, attempt_id, status):
        with self.lock:
            run = self.transcripts.load(identity).run
            readings = tuple(r.evolve(status=Status.RUNNING, attempts=tuple(
                a.evolve(status=status) if a.id == attempt_id else a for a in r.attempts)) for r in run.readings)
            self.transcripts.checkpoint_run(run.evolve(status=Status.RUNNING, readings=readings))

    def _result(self, identity, attempt_id, context, result):
        with self.lock:
            data = self._load(identity)
            progress = data['attempts'][str(attempt_id)]
            progress['results'].append(dict(chunk_index=context.segment_index, transcript=result.model_dump(mode='json')))
            self._save(identity, data)
            self.transcripts.accept(FinalTranscriptEvent(context=context, span_id='file', span_index=0, revision=1, text=result.text))
            progress['completed_chunks'] += 1
            self._save(identity, data)

    def _finish(self, identity, attempt_id, status, errors=(), cleanup_errors=()):
        with self.lock:
            data = self._load(identity)
            data['attempts'][str(attempt_id)]['cleanup_errors'] = [e.model_dump(mode='json') for e in cleanup_errors]
            self._save(identity, data)
            self.transcripts.finalize(identity, attempt_id, status=status, errors=errors)
            run = self.transcripts.load(identity).run
            reading = run.readings[0]
            latest = {a.model_entry_id: a for a in sorted(reading.attempts, key=lambda a: a.number)}
            states = [a.status for a in latest.values()]
            overall = Status.COMPLETE if all(s == Status.COMPLETE for s in states) else (
                Status.INCOMPLETE if all(s in TERMINAL for s in states) else Status.RUNNING)
            self.transcripts.checkpoint_run(run.evolve(status=overall, readings=(reading.evolve(status=overall),)))

    async def _work(self, identity, attempt_id):
        completed = 0
        try:
            data = await asyncio.to_thread(self._load, identity)
            run = (await asyncio.to_thread(self.transcripts.load, identity)).run
            reading = run.readings[0]
            attempt = next(a for a in reading.attempts if a.id == attempt_id)
            model = next(m for m in run.models if m.entry_id == attempt.model_entry_id)
            preparation = data.get('preparation')
            if preparation is None:  # Earlier local DEMO fixture compatibility.
                _, preparation = await asyncio.to_thread(self.audio._load, 'prepared', data['preparation_id'])
            adapter = self.factory(model, attempt.number) if self.factory else (
                DemoAdapter(model.provider, attempt.number) if data['execution_mode'] == 'demo' else adapter_for(model.provider))
            await asyncio.to_thread(self._state, identity, attempt_id, Status.RUNNING)
            for segment, chunk in zip(reading.segments, preparation['chunks']):
                context = EventContext(run_id=identity, reading=reading.label, attempt_id=attempt_id,
                    model_entry_id=model.entry_id, segment_id=segment.id, segment_index=segment.index)
                path = await asyncio.to_thread(self.audio.prepared_path, data['preparation_id'], chunk['index'])
                async with asyncio.timeout(self.chunk_timeout):
                    result = await adapter.transcribe_file(FileRequest(context=context, model=model, audio_path=path,
                        mime_type='audio/wav', expected_audio_sha256=chunk['sha256']))
                await asyncio.to_thread(self._result, identity, attempt_id, context, result)
                completed += 1
            await asyncio.to_thread(self._finish, identity, attempt_id, Status.COMPLETE)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._finish, identity, attempt_id, Status.INTERRUPTED,
                (ErrorDetails(code=ErrorCode.DISCONNECTED, message='Izpilde pārtraukta; saglabāti saņemtie fragmenti.'),))
            raise
        except Exception as error:
            details = error.details if isinstance(error, ProviderFailure) else ErrorDetails(
                code=ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INTERNAL,
                message='Fragmenta izpildes laiks pārsniegts.' if isinstance(error, TimeoutError) else 'Faila izpildi nevar pabeigt.')
            try:
                await asyncio.to_thread(self._finish, identity, attempt_id,
                    Status.INCOMPLETE if completed else Status.FAILED, (details,), getattr(error, 'cleanup_errors', ()))
            except Exception:
                # Storage failure must remain visible, while sibling workers continue.
                self.storage_failures[(identity, attempt_id)] = 'Izpildes rezultātu nevar saglabāt.'

    def recover(self):
        for run in self.registry.list_runs():
            if not self._path(run.id).exists():
                continue
            try:
                current = self.transcripts.load(run.id).run
            except Exception:
                continue
            for reading in current.readings:
                for attempt in reading.attempts:
                    if attempt.status not in TERMINAL:
                        try:
                            self._finish(run.id, attempt.id, Status.INTERRUPTED, (ErrorDetails(
                                code=ErrorCode.DISCONNECTED, message='Serveris pārstartēts. Automātiska audio atkārtošana nenotiek.'),))
                        except Exception:
                            self.storage_failures[(run.id, attempt.id)] = 'Pārtrauktā mēģinājuma saglabāšana neizdevās.'

    def view(self, identity):
        with self.lock:
            data = self._load(identity)
            checkpoint = self.transcripts.load(identity)
            return {**data, 'run': checkpoint.run.model_dump(mode='json'), 'transcripts': {
                str(s.attempt_id): finalized_text(s) for s in checkpoint.attempts},
                'active_attempts': [str(a) for (r, a) in list(self.tasks) if r == identity],
                'storage_failures': {str(a): text for (r, a), text in self.storage_failures.items() if r == identity}}

    async def retry(self, identity, model_id):
        def prepare():
            with self.lock:
                data = self._load(identity)
                self._check_budget(data['execution_mode'])
                run = self.transcripts.load(identity).run
                reading = run.readings[0]
                previous = [a for a in reading.attempts if a.model_entry_id == model_id]
                if not previous:
                    raise RegistryError('not_found', 'Modelis nav šajā izpildē.')
                if max(previous, key=lambda a: a.number).status not in TERMINAL:
                    raise RegistryError('conflict', 'Modeļa izpilde vēl nav pabeigta.')
                attempt = AttemptRecord(model_entry_id=model_id, number=max(a.number for a in previous) + 1,
                    segment_ids=tuple(s.id for s in reading.segments))
                self.transcripts.checkpoint_run(run.evolve(status=Status.RUNNING, readings=(reading.evolve(
                    status=Status.RUNNING, attempts=(*reading.attempts, attempt)),)))
                data['attempts'][str(attempt.id)] = dict(completed_chunks=0, results=[], storage_error=None)
                self._save(identity, data)
                return attempt.id
        attempt_id = await asyncio.to_thread(prepare)
        self._schedule(identity, attempt_id)
        return await asyncio.to_thread(self.view, identity)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
