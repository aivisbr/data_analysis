"""Registry endpoints; creating a run records selection, it does not transcribe."""

from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from typing import Literal
from pydantic import Field

from app.contracts import Mode, ModelEntry, Provider, Record, RunRecord, TranscriptionSettings
from app.providers.capabilities import CAPABILITY_PROFILES, CapabilityProfile
from app.registry import ModelRegistry, RegistryError
from app.providers.settings import resolve_language


router = APIRouter(prefix="/api")


class ModelCreate(Record):
    provider: Provider
    model_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    mode: Mode
    capability_profile_id: str
    enabled: bool = True
    order: int = Field(default=0, ge=0)
    settings: TranscriptionSettings = Field(default_factory=TranscriptionSettings)


class ModelPatch(Record):
    provider: Provider | None = None
    model_id: str | None = None
    display_name: str | None = None
    mode: Mode | None = None
    capability_profile_id: str | None = None
    enabled: bool | None = None
    order: int | None = None
    settings: TranscriptionSettings | None = None


class RunCreate(Record):
    mode: Mode


class LanguageRequest(Record):
    provider: Provider
    language_hint: str | None


@router.post("/language-preview")
def language_preview(payload: LanguageRequest):
    from app.registry import RegistryError
    try:
        # Validate hint syntax before provider mapping.
        settings = TranscriptionSettings(language_hint=payload.language_hint)
        return {"language_code": resolve_language(payload.provider, settings.language_hint)}
    except ValueError:
        raise RegistryError("invalid", "Valodas kodu nevar izmantot. Norādiet ISO 639-1 vai BCP-47 kodu.") from None


def registry(request: Request) -> ModelRegistry:
    return request.app.state.registry


@router.get("/models", response_model=list[ModelEntry])
def list_models(request: Request, mode: Mode | None = None):
    return registry(request).list(mode)


@router.get("/models/{entry_id}", response_model=ModelEntry)
def get_model(entry_id: UUID, request: Request):
    return registry(request).get(entry_id)


@router.post("/models", response_model=ModelEntry, status_code=201)
def add_model(payload: ModelCreate, request: Request):
    return registry(request).add(ModelEntry.model_validate(payload.model_dump()))


@router.patch("/models/{entry_id}", response_model=ModelEntry)
def update_model(entry_id: UUID, payload: ModelPatch, request: Request):
    # Omitted fields remain unchanged; an explicit null is rejected by ModelEntry validation.
    return registry(request).update(entry_id, payload.model_dump(exclude_unset=True))


@router.delete("/models/{entry_id}", status_code=204)
def delete_model(entry_id: UUID, request: Request):
    registry(request).delete(entry_id)
    return Response(status_code=204)


@router.get("/capabilities", response_model=list[CapabilityProfile])
def list_capabilities():
    return CAPABILITY_PROFILES


@router.post("/runs", response_model=RunRecord, status_code=201)
def create_run(payload: RunCreate, request: Request):
    run = registry(request).create_run(payload.mode)
    request.app.state.transcripts.checkpoint_run(run)
    return run


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: UUID, request: Request):
    if request.app.state.transcripts.exists(run_id):
        return request.app.state.transcripts.load(run_id).run
    return registry(request).get_run(run_id)


@router.get("/runs/{run_id}/attempts/{attempt_id}/files/{kind}")
def download_transcript(run_id: UUID, attempt_id: UUID,
                        kind: Literal["raw", "editable"], request: Request):
    # Confirm historical run identity first; registry deletion does not erase it.
    registry(request).get_run(run_id)
    try:
        path = request.app.state.transcripts.export_path(run_id, attempt_id, kind)
    except ValueError:
        raise RegistryError("not_found", "Mēģinājuma fails nav atrasts.") from None
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)
