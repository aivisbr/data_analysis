"""Application factory. No provider SDK imports or network calls on startup."""

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from app import __version__
from app.config import Settings, load_settings
from app.api import router
from app.registry import ModelRegistry, RegistryError
from app.transcripts import TranscriptStore, TranscriptStorageError
from app.evaluation_inputs import EvaluationInputStore, EvaluationInputError
from app.evaluation_api import router as evaluation_router
from app.scoring import LegacyScoringStore
from app.diagnostics import DiagnosticStore
from app.results import ResultsStore
from app.audio import AudioStore, AudioError
from app.audio_api import router as audio_router
from app.file_jobs import FileJobs
from app.file_job_api import router as file_job_router
from app.live_api import router as live_router
from app.live_history import LiveHistory


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings.prepare_storage()
        application.state.settings = settings
        application.state.registry = ModelRegistry(settings.data_dir / "app.sqlite3")
        application.state.registry.initialize()
        application.state.transcripts = TranscriptStore(settings.runs_dir)
        application.state.audio = AudioStore(settings.data_dir / 'audio')
        application.state.live_distributions = set()
        application.state.live_history = LiveHistory(application.state.transcripts)
        await asyncio.to_thread(application.state.live_history.recover)
        application.state.file_jobs = FileJobs(application.state.registry, application.state.transcripts,
            application.state.audio, paid_enabled=settings.allow_paid_transcription)
        await asyncio.to_thread(application.state.file_jobs.recover)
        application.state.evaluation_inputs = EvaluationInputStore(
            settings.data_dir / "evaluation_inputs", settings.evaluations_dir,
            application.state.transcripts)
        application.state.scoring = LegacyScoringStore(application.state.evaluation_inputs)
        application.state.diagnostics = DiagnosticStore(application.state.evaluation_inputs)
        application.state.results = ResultsStore(application.state.evaluation_inputs,
            application.state.scoring, application.state.diagnostics)
        try:
            yield
        finally:
            await asyncio.gather(*(d.stop(interrupted=True) for d in list(application.state.live_distributions)
                if d.run and not d.stopped),return_exceptions=True)
            await application.state.file_jobs.close()

    application = FastAPI(
        title="Latviešu valodas diktāts", version=__version__, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    @application.exception_handler(RegistryError)
    async def registry_error(_request: Request, exc: RegistryError):
        codes = {"not_found": 404, "invalid": 422, "conflict": 409, "storage": 503}
        return JSONResponse({"detail": str(exc)}, status_code=codes[exc.code])

    @application.exception_handler(TranscriptStorageError)
    async def transcript_error(_request: Request, exc: TranscriptStorageError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @application.exception_handler(AudioError)
    async def audio_error(_request: Request, exc: AudioError):
        return JSONResponse({'detail': str(exc)}, status_code={
            'invalid': 422, 'not_found': 404, 'storage': 503, 'unavailable': 503}[exc.code])

    @application.exception_handler(EvaluationInputError)
    async def evaluation_input_error(_request: Request, exc: EvaluationInputError):
        codes = {"invalid": 422, "not_found": 404, "storage": 503}
        return JSONResponse({"detail": str(exc)}, status_code=codes[exc.code])

    @application.exception_handler(RequestValidationError)
    @application.exception_handler(ValidationError)
    async def validation_error(_request: Request, _exc):
        # FastAPI's default echoes rejected input, potentially including an accidental key.
        return JSONResponse({"detail": "Pieprasījuma dati nav derīgi."}, status_code=422)

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        messages = {404: "Lapa nav atrasta.", 405: "Šī darbība nav pieejama."}
        return JSONResponse(
            {"detail": messages.get(exc.status_code, "Pieprasījumu nevar izpildīt.")},
            status_code=exc.status_code, headers=exc.headers,
        )

    @application.get("/api/health")
    async def health():
        # Deliberately disclose neither environment values nor local file paths.
        return {"status": "ok", "version": __version__}

    @application.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    application.include_router(router)
    application.include_router(evaluation_router)
    application.include_router(audio_router)
    application.include_router(file_job_router)
    application.include_router(live_router)
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application
