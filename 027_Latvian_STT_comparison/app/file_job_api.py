from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Request
from app.contracts import Record

router = APIRouter(prefix='/api')

class JobCreate(Record):
    preparation_id: UUID
    execution_mode: Literal['demo', 'paid'] = 'demo'

@router.get('/file-jobs')
def history(request: Request):
    jobs = request.app.state.file_jobs
    return {'paid_enabled': jobs.paid_enabled, 'runs': [str(r.id) for r in jobs.registry.list_runs()
        if jobs._path(r.id).exists()]}

@router.post('/file-jobs', status_code=202)
async def start(payload: JobCreate, request: Request):
    return await request.app.state.file_jobs.start(payload.preparation_id, payload.execution_mode)

@router.get('/file-jobs/{identity}')
def view(identity: UUID, request: Request):
    return request.app.state.file_jobs.view(identity)

@router.post('/file-jobs/{identity}/retry/{model_id}', status_code=202)
async def retry(identity: UUID, model_id: UUID, request: Request):
    return await request.app.state.file_jobs.retry(identity, model_id)
