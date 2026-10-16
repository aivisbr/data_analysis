"""Text selection, snapshots, legacy scoring and separate text diagnostics."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from app.transcripts import TranscriptStorageError

from app.contracts import ReadingLabel
from app.evaluation_inputs import (
    EvaluationInputError, EvaluationRequest, EvaluationSnapshot, MAX_TEXT_BYTES, UploadedInput,
)
from app.transcripts import READING_DIRECTORIES
from app.scoring import LegacyResults
from app.diagnostics import DiagnosticResults


router = APIRouter(prefix="/api")


@router.post("/evaluation-inputs", response_model=UploadedInput, status_code=201)
def upload_text(request: Request, file: UploadFile = File(...),
                role: Literal["ground_truth", "hypothesis"] = Form(...),
                reading: ReadingLabel | None = Form(None),
                variant: Literal["raw", "edited"] | None = Form(None),
                label: str | None = Form(None)):
    try:
        payload = file.file.read(MAX_TEXT_BYTES + 1)
        return request.app.state.evaluation_inputs.upload(file.filename or "", payload,
            role=role, reading=reading, variant=variant, label=label)
    except OSError:
        raise EvaluationInputError() from None
    finally:
        file.file.close()


@router.get("/evaluation-inputs")
def list_text_inputs(request: Request):
    store = request.app.state.evaluation_inputs
    return [{**item.model_dump(mode="json"),
             "disk_path": str(store._path(store.inputs_root, item.id, "input.txt"))}
            for item in store.list_uploads()]


@router.get("/runs/{run_id}/evaluation-inputs")
def list_managed_inputs(run_id: UUID, request: Request):
    request.app.state.registry.get_run(run_id)
    store = request.app.state.transcripts
    if not store.exists(run_id):
        return []
    run = store.load(run_id).run
    entries = []
    for reading in run.readings:
        for attempt in reading.attempts:
            if attempt.save_status != "saved":
                continue
            model = next(m for m in run.models if m.entry_id == attempt.model_entry_id)
            for variant in ("raw", "edited"):
                kind = "raw" if variant == "raw" else "editable"
                entries.append({"selection": {"kind": "managed", "run_id": str(run_id),
                    "attempt_id": str(attempt.id), "variant": variant},
                    "label": model.display_name, "reading": reading.label,
                    "directory": READING_DIRECTORIES[reading.label],
                    "attempt_number": attempt.number, "processing_status": attempt.status,
                    "disk_path": str(store.export_path(run_id, attempt.id, kind))})
    return entries


@router.post("/evaluations", response_model=EvaluationSnapshot, status_code=201)
def create_evaluation(payload: EvaluationRequest, request: Request):
    try:
        return request.app.state.evaluation_inputs.create_snapshot(payload)
    except ValueError:
        raise EvaluationInputError("Izvēlētais transkripts nav atrasts.", code="not_found") from None


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationSnapshot)
def get_evaluation(evaluation_id: UUID, request: Request):
    return request.app.state.evaluation_inputs.load(evaluation_id)


@router.post("/evaluations/{evaluation_id}/legacy-scores", response_model=LegacyResults)
def score_evaluation(evaluation_id: UUID, request: Request):
    return request.app.state.scoring.score(evaluation_id)


@router.get("/evaluations/{evaluation_id}/legacy-scores", response_model=LegacyResults)
def get_legacy_scores(evaluation_id: UUID, request: Request):
    return request.app.state.scoring.load(evaluation_id)


@router.post("/evaluations/{evaluation_id}/diagnostics", response_model=DiagnosticResults)
def score_diagnostics(evaluation_id: UUID, request: Request):
    return request.app.state.diagnostics.score(evaluation_id)


@router.get("/evaluations/{evaluation_id}/diagnostics", response_model=DiagnosticResults)
def get_diagnostics(evaluation_id: UUID, request: Request):
    return request.app.state.diagnostics.load(evaluation_id)


@router.get("/evaluation-catalog")
def evaluation_catalog(request: Request):
    uploads = list_text_inputs(request)
    managed, unavailable = [], []
    for run in request.app.state.registry.list_runs():
        try:
            managed.extend(list_managed_inputs(run.id, request))
        except TranscriptStorageError:
            unavailable.append(str(run.id))
    return {"uploads": uploads, "managed": managed, "unavailable_runs": unavailable}


@router.get("/evaluations")
def evaluation_history(request: Request):
    return request.app.state.results.history()


@router.post("/evaluations/{evaluation_id}/results")
def complete_results(evaluation_id: UUID, request: Request):
    return request.app.state.results.calculate(evaluation_id)


@router.get("/evaluations/{evaluation_id}/results")
def get_results(evaluation_id: UUID, request: Request):
    return request.app.state.results.bundle(evaluation_id)


@router.get("/evaluations/{evaluation_id}/exports/{kind}")
def export_results(evaluation_id: UUID, kind: Literal["json", "csv"], request: Request):
    path = request.app.state.results.export_path(evaluation_id, kind)
    return FileResponse(path, filename=f"diktats_{evaluation_id}.{kind}",
                        media_type="application/json" if kind == "json" else "text/csv; charset=utf-8")
