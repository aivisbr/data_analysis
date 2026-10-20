"""SQLite model registry and immutable run selections. No provider calls."""

from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from pydantic import ValidationError

from app.contracts import Mode, ModelEntry, ModelSnapshot, Provider, RunRecord
from app.providers.capabilities import CAPABILITY_PROFILES, Support, get_profile
from app.providers.settings import effective_settings


class RegistryError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_entry(entry: ModelEntry):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", entry.model_id):
        raise RegistryError("invalid", "Modeļa identifikators nav derīgs.")
    model_id = entry.model_id.lower()
    legacy_audio = r"(?:gpt-realtime(?:-mini)?|gpt-audio(?:-mini)?|gpt-4o(?:-mini)?-(?:realtime|audio))(?:-preview)?(?:-\d{4}-\d{2}-\d{2})?"
    if entry.provider == Provider.OPENAI and (
        model_id.startswith(("whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"))
        or re.fullmatch(legacy_audio, model_id)
    ):
        raise RegistryError("invalid", "Novecojušus modeļus nevar izmantot.")
    try:
        profile = get_profile(entry.capability_profile_id)
    except ValueError:
        raise RegistryError("invalid", "Modeļa iespēju profils nav atrasts.") from None
    if profile.provider != entry.provider or profile.mode != entry.mode:
        raise RegistryError("invalid", "Profils neatbilst modeļa nodrošinātājam vai režīmam.")
    known = [p for p in CAPABILITY_PROFILES if p.provider == entry.provider and p.model_id == entry.model_id]
    if known and not any(p.id == profile.id for p in known):
        raise RegistryError("invalid", "Zināmā modeļa profils vai režīms neatbilst izvēlei.")
    settings = entry.settings
    try:
        effective_settings(entry.provider, settings)
    except ValueError as exc:
        raise RegistryError("invalid", str(exc)) from None
    if entry.mode == Mode.FILE and settings.turn_detection != "default":
        raise RegistryError("invalid", "Faila režīmā runas posmu vadību nevar iestatīt.")
    if entry.provider == Provider.OPENAI and any("<" in term or ">" in term for term in settings.keywords):
        raise RegistryError("invalid", "OpenAI atslēgvārdi nedrīkst saturēt leņķiekavas.")
    if entry.provider == Provider.GEMINI and len(settings.keywords) > 1000:
        raise RegistryError("invalid", "Gemini vārdnīcā var būt līdz 1000 frāzēm.")
    if entry.provider == Provider.ELEVENLABS and entry.mode == Mode.LIVE and (
        len(settings.keywords) > 50 or any(len(term) > 20 for term in settings.keywords)
    ):
        raise RegistryError("invalid", "ElevenLabs tiešraidei var norādīt līdz 50 frāzēm, katru līdz 20 rakstzīmēm.")
    options = (
        (settings.language_hint is not None, profile.language),
        (bool(settings.keywords), profile.keywords),
        (settings.prompt is not None, profile.prompt),
        (settings.latency is not None, profile.latency),
        (settings.formatting != "default", profile.formatting),
        (settings.turn_detection == "automatic", profile.automatic_turn_detection),
    )
    if any(requested and support != Support.SUPPORTED for requested, support in options):
        raise RegistryError("invalid", "Iestatījums šim modelim nav atbalstīts vai vēl nav pārbaudīts.")
    return profile


class ModelRegistry:
    def __init__(self, database: Path):
        self.database = database

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(self.database, timeout=5)
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        except sqlite3.Error:
            raise RegistryError("storage", "Modeļu datubāze nav pieejama. Mēģiniet vēlreiz.") from None
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS registry_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = db.execute("SELECT value FROM registry_metadata WHERE key='schema_version'").fetchone()
            if version is not None and version[0] != "1":
                raise RegistryError("storage", "Datubāzes versija šai lietotnei nav atbalstīta.")
            db.execute("CREATE TABLE IF NOT EXISTS model_entries (id TEXT PRIMARY KEY, mode TEXT NOT NULL, position INTEGER NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS model_mode_order ON model_entries(mode, position, id)")
            db.execute("CREATE TABLE IF NOT EXISTS run_snapshots (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO registry_metadata VALUES ('schema_version', '1')")
            seeded = db.execute("SELECT value FROM registry_metadata WHERE key='defaults_seeded'").fetchone()
            if seeded is None:
                # Seed only an empty initial registry, not a preexisting user-populated table.
                if db.execute("SELECT COUNT(*) FROM model_entries").fetchone()[0] == 0:
                    for index, profile in enumerate(CAPABILITY_PROFILES):
                        entry = ModelEntry(provider=profile.provider, model_id=profile.model_id,
                                           display_name=profile.model_id, mode=profile.mode,
                                           capability_profile_id=profile.id, order=index,
                                           enabled=profile.provider != Provider.MISTRAL)
                        validate_entry(entry)
                        self._insert(db, entry)
                db.execute("INSERT INTO registry_metadata VALUES ('defaults_seeded', '1')")
            # One-time additive upgrade: preserve all existing selections and history.
            # Keep a separate marker so deleting these defaults never resurrects them.
            if db.execute("SELECT 1 FROM registry_metadata WHERE key='mistral_v3_seeded'").fetchone() is None:
                rows = db.execute('SELECT payload FROM model_entries').fetchall()
                entries = [self._decode(ModelEntry, row[0]) for row in rows]
                present = {(entry.provider, entry.model_id, entry.mode) for entry in entries}
                position = max((entry.order for entry in entries), default=-1) + 1
                for profile in CAPABILITY_PROFILES:
                    if profile.provider == Provider.MISTRAL and (profile.provider, profile.model_id, profile.mode) not in present:
                        entry = ModelEntry(provider=profile.provider, model_id=profile.model_id,
                            display_name=profile.model_id, mode=profile.mode, enabled=False,
                            capability_profile_id=profile.id, order=position)
                        validate_entry(entry)
                        self._insert(db, entry)
                        position += 1
                db.execute("INSERT INTO registry_metadata VALUES ('mistral_v3_seeded', '1')")

    @staticmethod
    def _insert(db, entry: ModelEntry):
        db.execute("INSERT INTO model_entries (id,mode,position,payload) VALUES (?,?,?,?)",
                   (str(entry.id), entry.mode.value, entry.order, entry.model_dump_json()))

    @staticmethod
    def _decode(record_type, payload):
        try:
            return record_type.model_validate_json(payload)
        except ValidationError:
            raise RegistryError("storage", "Saglabātos datus nevar nolasīt.") from None

    @staticmethod
    def _row(db, entry_id):
        row = db.execute("SELECT payload FROM model_entries WHERE id=?", (str(entry_id),)).fetchone()
        if row is None:
            raise RegistryError("not_found", "Modelis nav atrasts.")
        return row

    def list(self, mode: Mode | None = None) -> tuple[ModelEntry, ...]:
        with self._transaction() as db:
            if mode is None:
                rows = db.execute("SELECT payload FROM model_entries ORDER BY mode,position,id").fetchall()
            else:
                rows = db.execute("SELECT payload FROM model_entries WHERE mode=? ORDER BY position,id", (mode.value,)).fetchall()
            return tuple(self._decode(ModelEntry, row[0]) for row in rows)

    def get(self, entry_id: UUID) -> ModelEntry:
        with self._transaction() as db:
            return self._decode(ModelEntry, self._row(db, entry_id)[0])

    def add(self, entry: ModelEntry) -> ModelEntry:
        validate_entry(entry)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM model_entries WHERE id=?", (str(entry.id),)).fetchone():
                raise RegistryError("conflict", "Modelis ar šo identifikatoru jau pastāv.")
            self._insert(db, entry)
        return entry

    def update(self, entry_id: UUID, changes: dict) -> ModelEntry:
        if "id" in changes:
            raise RegistryError("invalid", "Modeļa identifikatoru nevar mainīt.")
        with self._transaction() as db:
            previous = self._decode(ModelEntry, self._row(db, entry_id)[0])
            try:
                updated = previous.evolve(**changes)
            except ValidationError:
                raise RegistryError("invalid", "Modeļa dati nav derīgi.") from None
            validate_entry(updated)
            db.execute("UPDATE model_entries SET mode=?,position=?,payload=? WHERE id=?",
                       (updated.mode.value, updated.order, updated.model_dump_json(), str(entry_id)))
        return updated

    def delete(self, entry_id: UUID) -> None:
        with self._transaction() as db:
            if db.execute("DELETE FROM model_entries WHERE id=?", (str(entry_id),)).rowcount == 0:
                raise RegistryError("not_found", "Modelis nav atrasts.")
        # No filesystem deletion or foreign-key cascade into historical selections.

    def create_run(self, mode: Mode, *, demo: bool = False) -> RunRecord:
        with self._transaction() as db:
            rows = db.execute("SELECT payload FROM model_entries WHERE mode=? ORDER BY position,id", (mode.value,)).fetchall()
            entries = [self._decode(ModelEntry, row[0]) for row in rows]
            snapshots = []
            for entry in entries:
                if entry.enabled:
                    profile = validate_entry(entry)
                    snapshots.append(ModelSnapshot.from_entry(
                        entry, effective=effective_settings(entry.provider, entry.settings)
                    ).evolve(capability_profile_version=profile.version))
            if not snapshots:
                raise RegistryError("conflict", "Šim režīmam nav ieslēgts neviens modelis.")
            if demo:
                snapshots = [m.evolve(display_name=m.display_name + ' [DEMO]') for m in snapshots]
            run = RunRecord(mode=mode, models=tuple(snapshots))
            db.execute("INSERT INTO run_snapshots VALUES (?,?)", (str(run.id), run.model_dump_json()))
            return run

    def get_run(self, run_id: UUID) -> RunRecord:
        with self._transaction() as db:
            row = db.execute("SELECT payload FROM run_snapshots WHERE id=?", (str(run_id),)).fetchone()
            if row is None:
                raise RegistryError("not_found", "Izpilde nav atrasta.")
            return self._decode(RunRecord, row[0])

    def list_runs(self) -> tuple[RunRecord, ...]:
        with self._transaction() as db:
            runs = tuple(self._decode(RunRecord, row[0]) for row in db.execute("SELECT payload FROM run_snapshots"))
            return tuple(sorted(runs, key=lambda run: run.created_at, reverse=True))
