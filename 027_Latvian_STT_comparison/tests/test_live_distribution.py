import asyncio
from array import array
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import wave

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.registry import ModelRegistry
from app.transcripts import TranscriptStore
from app.contracts import Mode, Provider, FinalTranscriptEvent
from app.live_distribution import LiveDistribution, LiveStart, FrameHeader, PCMConverter, FakeLiveSession


def header(segment,index,samples=480,rate=48000,channels=2,base=96000):
    return FrameHeader(sequence=index,segment_id=segment,sample_rate=rate,channels=channels,frame_samples=samples,
        source_start_sample=base+index*samples,source_end_sample=base+(index+1)*samples,
        audio_start_sample=index*samples,audio_end_sample=(index+1)*samples)

class ConverterTests(unittest.TestCase):
    def test_constant_signal_mix_and_fractional_chunk_invariance(self):
        source=array('h',[1000,3000]*441).tobytes()
        converter=PCMConverter(44100,16000,2)
        output=converter.convert(source)+converter.convert(b'',final=True)
        values=array('h');values.frombytes(output)
        self.assertEqual(len(values),160);self.assertEqual(set(values),{2000})
        for step in (4,28,100):
            converter=PCMConverter(44100,16000,2)
            parts=[converter.convert(source[i:i+step]) for i in range(0,len(source),step)]
            self.assertEqual(b''.join(parts)+converter.convert(b'',final=True),output)
    def test_integer_downsample_and_final_partial(self):
        converter=PCMConverter(48000,24000,1)
        values=array('h',[0,100,200,300,400])
        output=array('h');output.frombytes(converter.convert(values.tobytes())+converter.convert(b'',final=True))
        self.assertEqual(list(output),[50,250,400])

class DistributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_failure_is_storage_and_other_workers_finish(self):
        from unittest.mock import patch
        from app.transcripts import TranscriptStorageError
        original=self.store.accept
        failed_id=self.registry.list(Mode.LIVE)[0].id
        def accept(event):
            if event.context.model_entry_id==failed_id:raise TranscriptStorageError()
            return original(event)
        pipeline=LiveDistribution(self.registry,self.store)
        await pipeline.start(self.info)
        pipeline.offer(header(self.segment,0),self.pcm)
        with patch.object(self.store,'accept',side_effect=accept):result=await pipeline.stop()
        self.assertEqual(result['models'][str(failed_id)]['error']['code'],'storage')
        for identity,state in result['models'].items():
            if identity!=str(failed_id):self.assertEqual(state['status'],'complete')

    async def test_slow_final_is_not_cancelled_by_queued_send_deadline(self):
        class SlowFinal(FakeLiveSession):
            async def flush(inner):
                await asyncio.sleep(2.1)
                await super(SlowFinal,inner).flush()
        class Adapter:
            async def open_live(inner,request):return SlowFinal(request)
        for model in self.registry.list(Mode.LIVE)[1:]:self.registry.update(model.id,{'enabled':False})
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda model:Adapter(),timeout=.5)
        pipeline.final_timeout=3
        await pipeline.start(self.info)
        pipeline.offer(header(self.segment,0),self.pcm)
        result=await pipeline.stop()
        self.assertTrue(all(s['status']=='complete' for s in result['models'].values()),result)

    async def test_specific_flush_error_is_retained(self):
        from app.contracts import ErrorCode, ErrorDetails
        from app.providers.base import ProviderFailure
        class FailedFlush(FakeLiveSession):
            async def flush(inner):raise ProviderFailure(ErrorDetails(code=ErrorCode.AUTHENTICATION,message='Safe authentication failure'))
        class Adapter:
            async def open_live(inner,request):return FailedFlush(request)
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda model:Adapter())
        await pipeline.start(self.info)
        result=await pipeline.stop()
        self.assertTrue(all(s['error']['code']=='authentication' for s in result['models'].values()))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.registry=ModelRegistry(self.root/'app.sqlite3');self.registry.initialize()
        self.store=TranscriptStore(self.root/'runs');self.segment=uuid4()
        self.info=LiveStart(reading='D',segment_id=self.segment,sample_rate=48000,channels=2,source_start_sample=96000)
        self.pcm=array('h',[1000,3000]*480).tobytes()

    async def test_slow_worker_overflow_does_not_block_healthy_sequence(self):
        sessions={}
        class Session(FakeLiveSession):
            async def send_audio(inner,frame):
                if inner.request.model.provider==Provider.GEMINI:await asyncio.sleep(.2)
                inner.frames.append(frame)
        class Adapter:
            async def open_live(inner,request):
                session=Session(request);session.frames=[];sessions[request.model.entry_id]=session;return session
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda model:Adapter(),queue_size=3,timeout=.5)
        await pipeline.start(self.info)
        for i in range(10):
            pipeline.offer(header(self.segment,i),self.pcm)
            await asyncio.sleep(.015)
        result=await pipeline.stop()
        self.assertEqual(result['accepted_frames'],10)
        gemini=next(m for m in pipeline.run.models if m.provider==Provider.GEMINI)
        self.assertEqual(result['models'][str(gemini.entry_id)]['error']['code'],'queue_overflow')
        for model in pipeline.run.models:
            if model.provider!=Provider.GEMINI:
                self.assertEqual(result['models'][str(model.entry_id)]['sent_sequences'],list(range(10)))
                self.assertEqual(result['models'][str(model.entry_id)]['status'],'complete')
        with wave.open(str(pipeline.folder/'audio.wav')) as wav:
            self.assertEqual(wav.getnchannels(),2);self.assertEqual(wav.getframerate(),48000)
            self.assertEqual(wav.readframes(wav.getnframes()),self.pcm*10)
        self.assertEqual(len((pipeline.folder/'source_frames.jsonl').read_text().splitlines()),10)

    async def test_failed_connection_and_unchecked_model_are_isolated(self):
        disabled=self.registry.list(Mode.LIVE)[0];self.registry.update(disabled.id,{'enabled':False})
        class Adapter:
            async def open_live(inner,request):
                if request.model.provider==Provider.GEMINI:raise RuntimeError('PRIVATE key')
                return FakeLiveSession(request)
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda model:Adapter())
        ready=await pipeline.start(self.info)
        self.assertNotIn(str(disabled.id),ready['models'])
        pipeline.offer(header(self.segment,0),self.pcm)
        result=await pipeline.stop()
        self.assertTrue(any(s['status']=='complete' for s in result['models'].values()))
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertTrue(all(a.save_status=='saved' for a in self.store.load(pipeline.run.id).run.readings[0].attempts))

    async def test_source_gap_interrupts_and_preserves_prior_audio(self):
        pipeline=LiveDistribution(self.registry,self.store)
        await pipeline.start(self.info);pipeline.offer(header(self.segment,0),self.pcm)
        with self.assertRaises(ValueError):pipeline.offer(header(self.segment,2),self.pcm)
        result=await pipeline.stop()
        self.assertEqual(result['error']['code'],'invalid_audio')
        self.assertEqual(result['accepted_frames'],1)
        self.assertEqual(self.store.load(pipeline.run.id).run.status,'incomplete')

    async def test_pause_late_finals_resume_readiness_and_separate_readings(self):
        sessions=[];connecting=asyncio.Event();release=asyncio.Event()
        class Session(FakeLiveSession):
            async def send_audio(inner,frame):inner.received+=1
            async def flush(inner):
                await asyncio.sleep(.01)
                await inner.queue.put(FinalTranscriptEvent(context=inner.request.context,span_id='same',span_index=0,
                    revision=1,text=f'{inner.request.context.reading.value}-{inner.request.context.segment_index}'))
                await inner.queue.put(None)
            async def close(inner):inner.closed=True
        class Adapter:
            async def open_live(inner,request):
                if request.context.segment_index==1:
                    connecting.set();await release.wait()
                session=Session(request);session.closed=False;sessions.append(session);return session
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter())
        await pipeline.start(self.info)
        pipeline.offer(header(self.segment,0),self.pcm)
        paused=await pipeline.pause()
        self.assertEqual(paused['state'],'paused');self.assertTrue(all(s.closed for s in sessions))
        checkpoint=self.store.load(pipeline.run.id)
        self.assertTrue(all(not a.sealed for a in checkpoint.attempts))
        self.assertFalse(pipeline.offer(header(self.segment,1),self.pcm))
        self.assertEqual(pipeline.accepted,480)
        new=uuid4();resume_info=self.info.evolve(segment_id=new,source_start_sample=192000)
        resume_task=asyncio.create_task(pipeline.resume(resume_info));await connecting.wait()
        self.assertFalse(resume_task.done());self.assertEqual(pipeline.phase,'preparing')
        with self.assertRaises(ValueError):pipeline.offer(header(new,1,base=191520),self.pcm)
        release.set();await resume_task
        pipeline.offer(header(new,1,base=191520),self.pcm)
        await pipeline.stop()
        checkpoint=self.store.load(pipeline.run.id)
        self.assertEqual(len(checkpoint.run.readings[0].segments),2)
        for attempt in checkpoint.run.readings[0].attempts:
            self.assertEqual((self.store.root/str(pipeline.run.id)/attempt.raw_file).read_text(encoding='utf-8'),'D-0 D-1')
        with wave.open(str(pipeline.folder/'audio.wav')) as wav:
            self.assertEqual(wav.readframes(wav.getnframes()),self.pcm*2)
        other=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter())
        a_info=self.info.evolve(reading='A',segment_id=uuid4())
        await other.start(a_info);other.offer(header(a_info.segment_id,0),self.pcm);await other.stop()
        self.assertNotEqual(other.run.id,pipeline.run.id)
        for attempt in self.store.load(other.run.id).run.readings[0].attempts:
            self.assertEqual((self.store.root/str(other.run.id)/attempt.raw_file).read_text(encoding='utf-8'),'A-0')
        self.assertEqual(other.folder.name,'Try1');self.assertEqual(pipeline.folder.name,'Try2')
        # Open requests retain configured hints exactly, without appending prior reading text.
        original={m.entry_id:m for m in pipeline.run.models}
        self.assertTrue(all(s.request.model.requested_settings==original[s.request.model.entry_id].requested_settings
                            and s.request.model.effective_settings==original[s.request.model.entry_id].effective_settings
                            for s in sessions))

    async def test_resume_validation_and_stop_while_paused(self):
        pipeline=LiveDistribution(self.registry,self.store)
        await pipeline.start(self.info);pipeline.offer(header(self.segment,0),self.pcm)
        await pipeline.pause()
        for invalid in (self.info, self.info.evolve(reading='A',segment_id=uuid4()),
                        self.info.evolve(segment_id=uuid4(),sample_rate=16000)):
            with self.assertRaises(ValueError):await pipeline.resume(invalid)
            self.assertEqual(pipeline.phase,'paused')
        result=await pipeline.stop();self.assertEqual(result['state'],'stopped')
        self.assertEqual(len(self.store.load(pipeline.run.id).run.readings[0].segments),1)
        self.assertEqual(await pipeline.stop(),result)

    async def test_failed_worker_is_not_reconnected_on_resume(self):
        calls={}
        class Adapter:
            async def open_live(inner,request):
                identity=request.model.entry_id;calls[identity]=calls.get(identity,0)+1
                if request.model.provider==Provider.GEMINI:raise RuntimeError('private')
                return FakeLiveSession(request)
        pipeline=LiveDistribution(self.registry,self.store,factory=lambda m:Adapter())
        await pipeline.start(self.info);pipeline.offer(header(self.segment,0),self.pcm);await pipeline.pause()
        info=self.info.evolve(segment_id=uuid4(),source_start_sample=192000)
        await pipeline.resume(info);pipeline.offer(header(info.segment_id,1,base=191520),self.pcm)
        result=await pipeline.stop()
        for model in pipeline.run.models:
            self.assertEqual(calls[model.entry_id],1 if model.provider==Provider.GEMINI else 2)
            self.assertEqual(result['models'][str(model.entry_id)]['status'],
                'incomplete' if model.provider==Provider.GEMINI else 'complete')

class IngestApiTests(unittest.TestCase):
    def test_websocket_pause_resume_appends_segments_without_paused_audio(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(Settings(storage_root=Path(folder)))) as client:
            first=uuid4();second=uuid4()
            source={'reading':'A','segment_id':str(first),'sample_rate':48000,'channels':2,'source_start_sample':96000}
            pcm=array('h',[500,1500]*480).tobytes()
            with client.websocket_connect('/api/live-ingest') as socket:
                socket.send_json({'type':'start','source':source});self.assertEqual(socket.receive_json()['state'],'ready')
                socket.send_json({'type':'frame','header':header(first,0).model_dump(mode='json')});socket.send_bytes(pcm)
                socket.send_json({'type':'pause'});paused=socket.receive_json();self.assertEqual(paused['state'],'paused')
                socket.send_json({'type':'frame','header':header(first,1).model_dump(mode='json')});socket.send_bytes(pcm)
                socket.send_json({'type':'resume','source':{**source,'segment_id':str(second),'source_start_sample':192000}})
                resumed=socket.receive_json();self.assertEqual(resumed['state'],'ready');self.assertEqual(resumed['accepted_frames'],1)
                socket.send_json({'type':'frame','header':header(second,1,base=191520).model_dump(mode='json')});socket.send_bytes(pcm)
                socket.send_json({'type':'stop'});result=socket.receive_json()
                self.assertEqual(result['accepted_frames'],2);self.assertEqual(result['state'],'stopped')
                run=client.get('/api/runs/'+result['run_id']).json()
                self.assertEqual(len(run['readings'][0]['segments']),2);self.assertEqual(run['status'],'complete')

    def test_missing_browser_frames_cannot_claim_complete(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(Settings(storage_root=Path(folder)))) as client:
            with client.websocket_connect('/api/live-ingest') as socket:
                socket.send_json({'type':'start','source':{'reading':'D','segment_id':str(uuid4()),
                    'sample_rate':48000,'channels':2,'source_start_sample':0}});socket.receive_json()
                socket.send_json({'type':'stop','expected_frames':1,'expected_samples':480});result=socket.receive_json()
                self.assertEqual(result['error']['code'],'disconnected')
                self.assertEqual(client.get('/api/runs/'+result['run_id']).json()['status'],'incomplete')

    def test_websocket_binary_audio_archived_and_events_saved(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(Settings(storage_root=Path(folder)))) as client:
            segment=uuid4()
            with client.websocket_connect('/api/live-ingest') as socket:
                socket.send_json({'type':'start','source':{'reading':'A','segment_id':str(segment),
                    'sample_rate':48000,'channels':2,'source_start_sample':96000}})
                ready=socket.receive_json();self.assertEqual(ready['type'],'ready')
                socket.send_json({'type':'frame','header':header(segment,0).model_dump(mode='json')})
                pcm=array('h',[500,1500]*480).tobytes();socket.send_bytes(pcm)
                socket.send_json({'type':'stop'});result=socket.receive_json()
                self.assertEqual(result['type'],'stopped');self.assertEqual(result['accepted_frames'],1)
                run=client.get('/api/runs/'+result['run_id']).json()
                self.assertEqual(run['status'],'complete')
                self.assertTrue(all('[DEMO]' in m['display_name'] for m in run['models']))
                self.assertTrue((Path(folder)/'runs'/result['run_id']/'Try1'/'audio.wav').is_file())

if __name__=='__main__':unittest.main()
