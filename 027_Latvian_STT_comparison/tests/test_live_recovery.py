import asyncio
from array import array
import json
from pathlib import Path
import tempfile
import time
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.registry import ModelRegistry
from app.transcripts import TranscriptStore
from app.live_history import LiveHistory
from app.live_distribution import LiveDistribution, LiveStart, FakeLiveSession
from app.contracts import FinalTranscriptEvent, PartialTranscriptEvent
from test_live_distribution import header


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.registry=ModelRegistry(self.root/'app.sqlite3');self.registry.initialize()
        self.store=TranscriptStore(self.root/'runs');self.segment=uuid4()
        self.info=LiveStart(reading='A',segment_id=self.segment,sample_rate=48000,channels=2,source_start_sample=96000)
        self.pcm=array('h',[1000,3000]*480).tobytes()

    async def test_revisions_duplicates_late_final_and_provisional_not_exported(self):
        class Session(FakeLiveSession):
            async def flush(inner):
                context=inner.request.context
                last=FinalTranscriptEvent(context=context,span_id='last',span_index=2,revision=2,text='Last')
                for event in (last,last,
                    PartialTranscriptEvent(context=context,span_id='last',span_index=2,revision=1,text='stale'),
                    PartialTranscriptEvent(context=context,span_id='last',span_index=2,revision=99,text='must not unfinalize'),
                    PartialTranscriptEvent(context=context,span_id='pending',span_index=1,revision=1,text='Provisional')):
                    await inner.queue.put(event)
                await asyncio.sleep(.02)
                await inner.queue.put(FinalTranscriptEvent(context=context,span_id='first',span_index=0,revision=1,text='First'))
                await inner.queue.put(None)
        class Adapter:
            async def open_live(inner,request):return Session(request)
        # This test exercises ordering/revisions, not a short deadline; disk checkpoints
        # are serialized across models and can be slower in the full Windows suite.
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter(),timeout=5)
        await pipeline.start(self.info);pipeline.offer(header(self.segment,0),self.pcm);await pipeline.stop()
        view=LiveHistory(self.store).view(pipeline.run.id)
        for attempt in view['attempts']:
            self.assertEqual(attempt['final_text'],'First Last');self.assertEqual(attempt['provisional_text'],'Provisional')
            self.assertEqual(attempt['status'],'incomplete')
            raw=self.store.export_path(pipeline.run.id,UUID(attempt['attempt_id']),'raw')
            self.assertEqual(raw.read_text(encoding='utf-8'),'First Last')

    async def test_disconnection_drains_accepted_queue_and_late_final(self):
        sessions=[]
        class Session(FakeLiveSession):
            async def send_audio(inner,frame):
                await asyncio.sleep(.01);inner.received+=1
        class Adapter:
            async def open_live(inner,request):
                session=Session(request);sessions.append(session);return session
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter(),timeout=1)
        await pipeline.start(self.info)
        for index in range(3):pipeline.offer(header(self.segment,index),self.pcm)
        await pipeline.stop(interrupted=True)
        self.assertTrue(all(session.received==3 for session in sessions))
        self.assertTrue(all(a['final_text'] and a['status']=='incomplete' for a in LiveHistory(self.store).view(pipeline.run.id)['attempts']))

    async def test_whole_queue_and_flush_are_bounded(self):
        class Session(FakeLiveSession):
            async def send_audio(inner,frame):await asyncio.sleep(.035)
            async def flush(inner):await asyncio.Event().wait()
        class Adapter:
            async def open_live(inner,request):return Session(request)
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter(),timeout=.1)
        await pipeline.start(self.info)
        for index in range(20):pipeline.offer(header(self.segment,index),self.pcm)
        await asyncio.wait_for(pipeline.stop(),2)
        self.assertTrue(all(a['status']=='incomplete' for a in LiveHistory(self.store).view(pipeline.run.id)['attempts']))

    async def test_restart_recovers_unsealed_pause_preserves_edits_and_is_idempotent(self):
        pipeline=LiveDistribution(self.registry,self.store)
        await pipeline.start(self.info);pipeline.offer(header(self.segment,0),self.pcm);await pipeline.pause()
        # Simulate loss of in-memory orchestration after a durable pause; close OS handles only.
        await pipeline.source_queue.put(None);await pipeline.archive_task
        history=LiveHistory(self.store);history.recover()
        checkpoint=self.store.load(pipeline.run.id)
        self.assertEqual(checkpoint.run.status,'interrupted')
        self.assertTrue(all(a.status=='interrupted' and a.save_status=='saved' for a in checkpoint.run.readings[0].attempts))
        attempt=checkpoint.run.readings[0].attempts[0]
        editable=self.store.export_path(pipeline.run.id,attempt.id,'editable');editable.write_text('User edit',encoding='utf-8')
        before=(self.store.root/str(pipeline.run.id)/'metadata.json').read_bytes()
        history.recover()
        self.assertEqual((self.store.root/str(pipeline.run.id)/'metadata.json').read_bytes(),before)
        self.assertEqual(editable.read_text(encoding='utf-8'),'User edit')
        recovery=json.loads((self.store.root/str(pipeline.run.id)/'live_recovery.json').read_text(encoding='utf-8'))
        self.assertFalse(recovery['audio_replayed']);self.assertFalse(recovery['capture_reconnected'])

    async def test_long_pause_has_no_open_sessions_or_replay(self):
        sessions=[]
        class Session(FakeLiveSession):
            async def close(inner):inner.closed=True
        class Adapter:
            async def open_live(inner,request):
                session=Session(request);session.closed=False;sessions.append(session);return session
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter(),timeout=.5)
        await pipeline.start(self.info);pipeline.offer(header(self.segment,0),self.pcm);await pipeline.pause()
        self.assertTrue(all(s.closed for s in sessions))
        await asyncio.sleep(.6)  # Longer than the configured connection/send timeout.
        info=self.info.evolve(segment_id=uuid4(),source_start_sample=192000)
        await pipeline.resume(info);pipeline.offer(header(info.segment_id,1,base=191520),self.pcm);await pipeline.stop()
        self.assertTrue(all(s.received==1 for s in sessions))


class DisconnectApiTests(unittest.TestCase):
    def test_page_disconnect_finalizes_and_history_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(storage_root=Path(folder))
            with TestClient(create_app(settings)) as client:
                segment=uuid4()
                with client.websocket_connect('/api/live-ingest') as socket:
                    socket.send_json({'type':'start','source':{'reading':'D','segment_id':str(segment),
                        'sample_rate':48000,'channels':2,'source_start_sample':96000}})
                    identity=socket.receive_json()['run_id']
                    socket.send_json({'type':'frame','header':header(segment,0).model_dump(mode='json')})
                    socket.send_bytes(array('h',[1000,3000]*480).tobytes())
                data=client.get('/api/live-runs/'+identity).json()
                deadline=time.monotonic()+5
                while (data['status']=='running' or any(a['save_status']=='pending' for a in data['attempts'])) and time.monotonic()<deadline:
                    time.sleep(.02);data=client.get('/api/live-runs/'+identity).json()
                self.assertEqual(data['status'],'incomplete');self.assertTrue(all(a['files'] for a in data['attempts']))
            with TestClient(create_app(settings)) as client:
                self.assertIn(identity,[r['run_id'] for r in client.get('/api/live-runs').json()['runs']])
                data=client.get('/api/live-runs/'+identity).json()
                for attempt in data['attempts']:
                    self.assertEqual(client.get(attempt['files']['raw']).text,attempt['final_text'])
                self.assertFalse(client.app.state.live_distributions)

    def test_startup_recovers_live_checkpoint_but_leaves_partial_audio_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);registry=ModelRegistry(root/'app.sqlite3');registry.initialize();store=TranscriptStore(root/'runs')
            async def fixture():
                pipeline=LiveDistribution(registry,store)
                info=LiveStart(reading='A',segment_id=uuid4(),sample_rate=48000,channels=2,source_start_sample=0)
                await pipeline.start(info)
                # Write a final checkpoint event, but simulate loss of stop/finalization.
                for state in pipeline.workers.values():
                    store.accept(FinalTranscriptEvent(context=state['context'],span_id='one',span_index=0,revision=1,text='Retained'))
                await pipeline._drain_workers();await pipeline.source_queue.put(None);await pipeline.archive_task
                (pipeline.folder/'audio.wav').rename(pipeline.folder/'audio.partial.wav')
                return pipeline.run.id,pipeline.folder
            identity,audio_folder=asyncio.run(fixture());before=(audio_folder/'audio.partial.wav').read_bytes()
            with TestClient(create_app(Settings(storage_root=root))) as client:
                data=client.get('/api/live-runs/'+str(identity)).json()
                self.assertEqual(data['status'],'interrupted')
                self.assertTrue(all('Retained' in a['final_text'] for a in data['attempts']))
            self.assertEqual((audio_folder/'audio.partial.wav').read_bytes(),before)
            self.assertFalse((audio_folder/'audio.wav').exists())


if __name__=='__main__':unittest.main()
