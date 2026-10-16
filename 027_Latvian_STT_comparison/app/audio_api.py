"""Managed local audio upload, preview and immutable interval preparation."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from app.audio import PreparationRequest

router = APIRouter(prefix='/api')


@router.post('/audio-inputs', status_code=201)
def upload_audio(request: Request, file: UploadFile = File(...)):
    try:
        return request.app.state.audio.upload(file.filename or '', file.file)
    finally:
        file.file.close()


@router.get('/audio-inputs')
def list_audio(request: Request):
    return request.app.state.audio.list_uploads()


@router.get('/audio-inputs/{audio_id}/files/{kind}')
def audio_file(audio_id: UUID, kind: Literal['original', 'preview'], request: Request):
    path = request.app.state.audio.upload_path(audio_id, kind)
    return FileResponse(path, media_type='audio/wav' if kind == 'preview' else None)


@router.post('/audio-preparations', status_code=201)
def prepare_audio(payload: PreparationRequest, request: Request):
    return request.app.state.audio.prepare(payload)


@router.get('/audio-preparations/{preparation_id}')
def prepared_audio(preparation_id: UUID, request: Request):
    return request.app.state.audio._load('prepared', preparation_id)[1]


@router.get('/audio-preparations/{preparation_id}/preview')
def interval_preview(preparation_id: UUID, request: Request):
    return FileResponse(request.app.state.audio.prepared_path(preparation_id), media_type='audio/wav')


@router.get('/audio-preparations/{preparation_id}/chunks/{index}')
def chunk_audio(preparation_id: UUID, index: int, request: Request):
    return FileResponse(request.app.state.audio.prepared_path(preparation_id, index), media_type='audio/wav')
