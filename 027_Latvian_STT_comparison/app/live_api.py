"""Audio-only ingress with explicit DEMO/paid routing and disk-backed history."""
import asyncio
from uuid import UUID
from urllib.parse import urlparse
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import ValidationError
from app.live_distribution import LiveDistribution, LiveStart, LiveStop, FrameHeader
from app.providers.live_factory import paid_live_factory

router=APIRouter()

@router.get('/api/live-config')
def live_config(request: Request):
    settings=request.app.state.settings
    return {'paid_enabled':settings.allow_paid_live_transcription,
            'elevenlabs_enabled':settings.allow_paid_live_transcription and settings.allow_paid_elevenlabs_live}

@router.get('/api/live-runs')
def live_runs(request: Request):return request.app.state.live_history.list()

@router.get('/api/live-runs/{identity}')
def live_run(identity: UUID,request: Request):
    try:return request.app.state.live_history.view(identity)
    except ValueError:raise HTTPException(status_code=404) from None

@router.websocket('/api/live-ingest')
async def ingest(socket: WebSocket):
    origin=socket.headers.get('origin')
    if origin and urlparse(origin).hostname not in ('127.0.0.1','localhost','::1'):
        await socket.close(code=1008);return
    await socket.accept()
    distribution=None
    try:
        start=await socket.receive_json()
        if start.get('type')!='start':raise ValueError('start')
        mode=start.get('execution_mode','demo')
        if mode not in ('demo','paid'):raise ValueError('mode')
        settings=socket.app.state.settings
        if mode=='paid' and not settings.allow_paid_live_transcription:
            await socket.send_json({'type':'error','message':'Maksas tiešraide serverī nav iespējota.'})
            await socket.close(code=1008);return
        distribution=LiveDistribution(socket.app.state.registry,socket.app.state.transcripts,execution_mode=mode,
            factory=(lambda model:paid_live_factory(model,elevenlabs_enabled=settings.allow_paid_elevenlabs_live)) if mode=='paid' else None,
            timeout=25 if mode=='paid' else 5)
        socket.app.state.live_distributions.add(distribution)
        info=LiveStart.model_validate(start['source'])
        await socket.send_json({'type':'ready',**await distribution.start(info)})
        while True:
            message=await socket.receive_json()
            kind=message.get('type')
            if kind=='stop':
                stop=LiveStop.model_validate({k:v for k,v in message.items() if k!='type'})
                mismatch=(stop.expected_frames is not None and stop.expected_frames!=distribution.sequence or
                          stop.expected_samples is not None and stop.expected_samples!=distribution.accepted)
                await socket.send_json({'type':'stopped',**await distribution.stop(interrupted=stop.interrupted or mismatch)});break
            if kind=='pause':
                await socket.send_json({'type':'paused',**await distribution.pause()});continue
            if kind=='resume':
                info=LiveStart.model_validate(message['source'])
                await socket.send_json({'type':'ready',**await distribution.resume(info)});continue
            if kind=='status':await socket.send_json({'type':'status',**distribution.snapshot()});continue
            if kind!='frame':raise ValueError('frame')
            header=FrameHeader.model_validate(message['header'])
            payload=await socket.receive_bytes()
            if len(payload)>153600:raise ValueError('size')
            distribution.offer(header,payload)
    except WebSocketDisconnect:
        if distribution and distribution.run:await distribution.stop(interrupted=True)
    except (ValueError,ValidationError,KeyError,TypeError):
        if distribution and distribution.run:await distribution.stop(interrupted=True)
        await socket.send_json({'type':'error','message':'Audio dati nav derīgi vai izpilde ir nepilnīga.'})
        await socket.close(code=1008)
    except Exception:
        if distribution and distribution.run:
            try:await distribution.stop(interrupted=True)
            except Exception:pass
        try:await socket.send_json({'type':'error','message':'Audio izpildi nevar saglabāt vai noslēgt.'})
        except Exception:pass
        await socket.close(code=1011)
    finally:
        # Includes cancellation and failures while sending a response to a closing page.
        if distribution and distribution.run and not distribution.stopped:
            cleanup=asyncio.create_task(distribution.stop(interrupted=True))
            def cleanup_done(task):
                if not task.cancelled() and task.exception():
                    socket.app.state.live_history.recovery_errors[str(distribution.run.id)]='Izpildes noslēgšana neizdevās; saglabātos kontrolpunktus atkops pēc pārstartēšanas.'
                socket.app.state.live_distributions.discard(distribution)
            cleanup.add_done_callback(cleanup_done)
            try:await asyncio.shield(cleanup)
            except asyncio.CancelledError:pass  # Independent stop_task retains the save operation.
            except Exception:pass  # Startup recovery will use durable checkpoints.
        else:socket.app.state.live_distributions.discard(distribution)
