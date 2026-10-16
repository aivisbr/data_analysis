"""Local paths and environment loading; provider secrets stay in the environment."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    storage_root: Path
    allow_paid_transcription: bool = False
    allow_paid_live_transcription: bool = False
    allow_paid_elevenlabs_live: bool = False

    @property
    def data_dir(self) -> Path:
        return self.storage_root / "data"

    @property
    def runs_dir(self) -> Path:
        return self.storage_root / "runs"

    @property
    def evaluations_dir(self) -> Path:
        return self.storage_root / "evaluations"

    def prepare_storage(self) -> None:
        for directory in (self.data_dir, self.runs_dir, self.evaluations_dir):
            directory.mkdir(parents=True, exist_ok=True)


def load_settings(*, env_file: Path | None = PROJECT_ROOT / ".env") -> Settings:
    # Read only: never replace a key already supplied by the calling environment.
    if env_file is not None:
        load_dotenv(env_file, override=False, encoding="utf-8-sig")
    configured_root = os.environ.get("P7_STORAGE_ROOT", "").strip()
    storage_root = Path(configured_root).expanduser() if configured_root else PROJECT_ROOT
    if not storage_root.is_absolute():
        storage_root = PROJECT_ROOT / storage_root
    return Settings(storage_root=storage_root.resolve(),
        allow_paid_transcription=os.environ.get('P7_ALLOW_PAID_TRANSCRIPTION', '') == '1',
        allow_paid_live_transcription=os.environ.get('P7_ALLOW_PAID_LIVE_TRANSCRIPTION', '') == '1',
        allow_paid_elevenlabs_live=os.environ.get('P7_ALLOW_PAID_ELEVENLABS_LIVE', '') == '1')
