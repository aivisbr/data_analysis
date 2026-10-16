import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from uuid import UUID
import wave
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.audio import AudioStore, PreparationRequest
from app.config import Settings
from app.contracts import ErrorCode, ErrorDetails, Mode
from app.file_jobs import FileJobs
from app.main import create_app
from app.providers.base import FileTranscript, ProviderFailure
from app.registry import ModelRegistry, RegistryError
from app.transcripts import TranscriptStore


def audio_payload(seconds=2):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as wav:
        wav.setparams((1,2,24000,0,'NONE','not compressed'))
        wav.writeframes(b'\x00\x01'*round(seconds*24000))
    return stream.getvalue()


class FileJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.registry=ModelRegistry(self.root/'app.sqlite3');self.registry.initialize()
        self.store=TranscriptStore(self.root/'runs');self.audio=AudioStore(self.root/'audio')
        upload=self.audio.upload('test.wav',io.BytesIO(audio_payload()))
        self.prep=self.audio.prepare(PreparationRequest(audio_id=upload['id'],reading='A',chunk_seconds=1))
        self.jobs=FileJobs(self.registry,self.store,self.audio)

    async def drain(self,jobs=None):
        jobs=jobs or self.jobs
        await asyncio.gather(*list(jobs.tasks.values()))
        await asyncio.sleep(0)

    async def test_demo_failure_isolated_unchecked_and_retry_preserves_edits(self):
        models=self.registry.list(Mode.FILE)
        eleven=next(m for m in models if m.provider=='elevenlabs')
        self.registry.update(eleven.id,{'enabled':False})
        data=await self.jobs.start(UUID(self.prep['id']),'demo');identity=UUID(data['run_id'])
        await self.drain();data=self.jobs.view(identity)
        self.assertEqual(len(data['run']['models']),2)
        self.assertTrue(all(m['display_name'].endswith('[DEMO]') for m in data['run']['models']))
        attempts=data['run']['readings'][0]['attempts']
        good=next(a for a in attempts if a['status']=='complete');bad=next(a for a in attempts if a['status']=='failed')
        self.assertEqual(data['attempts'][good['id']]['completed_chunks'],2)
        edited=self.store.export_path(identity,UUID(bad['id']),'editable');edited.write_text('Mani labojumi',encoding='utf-8')
        raw=self.store.export_path(identity,UUID(bad['id']),'raw').read_bytes()
        await self.jobs.retry(identity,UUID(bad['model_entry_id']));await self.drain()
        self.assertEqual(self.jobs.view(identity)['run']['status'],'complete')
        self.assertEqual(edited.read_text('utf-8'),'Mani labojumi')
        self.assertEqual(self.store.export_path(identity,UUID(bad['id']),'raw').read_bytes(),raw)
        self.assertEqual(len(self.jobs.view(identity)['run']['readings'][0]['attempts']),3)

    async def test_provider_partial_timeout_and_shared_bytes(self):
        calls=[]
        class Fake:
            def __init__(self,model):self.model=model
            async def transcribe_file(self,request):
                calls.append((self.model.entry_id,request.context.segment_index,request.expected_audio_sha256,request.audio_path.read_bytes()))
                if self.model.provider=='gemini' and request.context.segment_index==1:
                    await asyncio.Event().wait()
                return FileTranscript(text='Vārds atkārtots atkārtots.')
        self.jobs=FileJobs(self.registry,self.store,self.audio,paid_enabled=True,
            factory=lambda model,number:Fake(model),chunk_timeout=.1)
        data=await self.jobs.start(UUID(self.prep['id']),'paid');identity=UUID(data['run_id']);await self.drain()
        data=self.jobs.view(identity)
        self.assertEqual(data['preparation']['chunks'],self.prep['chunks'])
        attempts=data['run']['readings'][0]['attempts']
        self.assertEqual(sorted(a['status'] for a in attempts),['complete','complete','incomplete'])
        partial=next(a for a in attempts if a['status']=='incomplete')
        self.assertEqual(partial['errors'][0]['code'],'timeout')
        self.assertEqual(self.store.export_path(identity,UUID(partial['id']),'raw').read_text('utf-8'),'Vārds atkārtots atkārtots.')
        for index in (0,1):
            frames=[c[3] for c in calls if c[1]==index]
            self.assertEqual(len(frames),3);self.assertTrue(all(f==frames[0] for f in frames))

    async def test_paid_gate_and_duplicate_retry(self):
        with self.assertRaises(RegistryError):await self.jobs.start(UUID(self.prep['id']),'paid')
        self.assertEqual(self.registry.list_runs(),())
        data=await self.jobs.start(UUID(self.prep['id']),'demo')
        identity=UUID(data['run_id']);model=UUID(data['run']['models'][0]['entry_id'])
        with self.assertRaises(RegistryError):await self.jobs.retry(identity,model)
        await self.drain()

    async def test_recovery_finalizes_partial_without_replaying_audio(self):
        identity,attempts=self.jobs._new(UUID(self.prep['id']),'demo')
        # Simulate abandoned jobs before workers were scheduled.
        self.jobs.recover()
        data=self.jobs.view(identity)
        self.assertTrue(all(a['status']=='interrupted' and a['save_status']=='saved' for a in data['run']['readings'][0]['attempts']))
        self.assertEqual(self.jobs.tasks,{})
        await self.jobs.retry(identity,attempts[0].model_entry_id);await self.drain()

    async def test_storage_failure_does_not_cancel_siblings(self):
        original=self.store.finalize
        fail_id=[]
        def broken(run_id,attempt_id,**kwargs):
            if attempt_id==fail_id[0]:raise OSError('PRIVATE')
            return original(run_id,attempt_id,**kwargs)
        data=await self.jobs.start(UUID(self.prep['id']),'demo');identity=UUID(data['run_id'])
        fail_id.append(UUID(data['run']['readings'][0]['attempts'][0]['id']))
        with patch.object(self.store,'finalize',side_effect=broken):await self.drain()
        data=self.jobs.view(identity)
        self.assertTrue(data['storage_failures'])
        self.assertTrue(any(a['save_status']=='saved' for a in data['run']['readings'][0]['attempts']))


class JobApiTests(unittest.TestCase):
    def test_api_history_download_catalog_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(storage_root=Path(folder))
            with TestClient(create_app(settings)) as client:
                uploaded=client.post('/api/audio-inputs',files={'file':('test.wav',audio_payload(),'audio/wav')}).json()
                prep=client.post('/api/audio-preparations',json={'audio_id':uploaded['id'],'reading':'D'}).json()
                self.assertEqual(client.post('/api/file-jobs',json={'preparation_id':prep['id'],'execution_mode':'paid'}).status_code,409)
                data=client.post('/api/file-jobs',json={'preparation_id':prep['id']}).json();identity=data['run_id']
                # Event-based waiting via the app loop, no provider network or fixed sleeps.
                client.portal.call(lambda: asyncio.gather(*list(client.app.state.file_jobs.tasks.values())))
                data=client.get('/api/file-jobs/'+identity).json()
                saved=data['run']['readings'][0]['attempts'][0]
                self.assertEqual(client.get(f"/api/runs/{identity}/attempts/{saved['id']}/files/raw").status_code,200)
                catalog=client.get('/api/evaluation-catalog').json()
                self.assertIn('[DEMO]',str(catalog))
            with TestClient(create_app(settings)) as client:
                self.assertIn(identity,client.get('/api/file-jobs').json()['runs'])
                self.assertEqual(client.get('/api/file-jobs/'+identity).json()['execution_mode'],'demo')
