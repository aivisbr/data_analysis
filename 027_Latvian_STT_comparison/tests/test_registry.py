"""Registry persistence, history and API validation; entirely offline."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import Mode, ModelEntry, TranscriptionSettings
from app.main import create_app
from app.registry import ModelRegistry, RegistryError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.registry = ModelRegistry(self.root / "data" / "app.sqlite3")
        self.registry.initialize()

    def tearDown(self):
        self.directory.cleanup()

    def test_initial_roster_and_independent_modes(self):
        live = self.registry.list(Mode.LIVE)
        files = self.registry.list(Mode.FILE)
        self.assertEqual(len(live), 6)
        self.assertEqual(len(files), 5)
        self.assertEqual(len({m.id for m in (*live, *files)}), 11)
        file_openai = next(m for m in files if m.model_id == "gpt-transcribe")
        self.registry.update(file_openai.id, {"enabled": False})
        live_openai = next(m for m in self.registry.list(Mode.LIVE) if m.model_id == "gpt-transcribe")
        self.assertTrue(live_openai.enabled)

    def test_edit_settings_and_order_survive_restart(self):
        original = self.registry.list(Mode.FILE)[0]
        updated = self.registry.update(original.id, {"display_name": "Variants ā", "order": 99,
            "settings": TranscriptionSettings(language_hint="lv", keywords=("Latvija",))})
        restarted = ModelRegistry(self.registry.database)
        restarted.initialize()
        self.assertEqual(restarted.get(original.id), updated)
        self.assertEqual(restarted.list(Mode.FILE)[-1], updated)

    def test_deleted_defaults_never_reappear_even_when_registry_empty(self):
        ids = [model.id for model in self.registry.list()]
        for entry_id in ids:
            self.registry.delete(entry_id)
        self.registry.initialize()
        restarted = ModelRegistry(self.registry.database)
        restarted.initialize()
        self.assertEqual(restarted.list(), ())

    def test_same_model_configuration_variants_allowed(self):
        first = self.registry.list(Mode.FILE)[0]
        variant = first.evolve(id=uuid4(), display_name="Otrs variants", settings=TranscriptionSettings(language_hint=None))
        self.registry.add(variant)
        self.assertEqual(len(self.registry.list(Mode.FILE)), 6)
        with self.assertRaises(RegistryError):
            self.registry.add(variant)

    def test_disabled_models_excluded_and_empty_selection_rejected(self):
        live = self.registry.list(Mode.LIVE)
        self.registry.update(live[0].id, {"enabled": False})
        run = self.registry.create_run(Mode.LIVE)
        self.assertNotIn(live[0].id, {m.entry_id for m in run.models})
        for model in live[1:]:
            self.registry.update(model.id, {"enabled": False})
        with self.assertRaises(RegistryError) as caught:
            self.registry.create_run(Mode.LIVE)
        self.assertEqual(caught.exception.code, "conflict")

    def test_historical_snapshots_and_files_survive_edit_delete_restart(self):
        run = self.registry.create_run(Mode.FILE)
        original = run.models[0]
        output = self.root / "runs" / str(run.id) / "A" / "raw.txt"
        output.parent.mkdir(parents=True)
        output.write_text("Sākotnējais teksts.", encoding="utf-8")
        self.registry.update(original.entry_id, {"settings": TranscriptionSettings(language_hint="en")})
        self.registry.delete(original.entry_id)
        restarted = ModelRegistry(self.registry.database)
        restarted.initialize()
        restored = restarted.get_run(run.id)
        self.assertEqual(restored, run)
        self.assertEqual(restored.models[0].requested_settings.language_hint, "lv-LV")
        self.assertEqual(restored.models[0].effective_settings.language_hint, "lv")
        self.assertEqual(output.read_text(encoding="utf-8"), "Sākotnējais teksts.")

    def test_deprecated_ids_and_profile_mismatches_are_rejected(self):
        base = next(m for m in self.registry.list(Mode.LIVE) if m.model_id == "gpt-transcribe")
        for model_id in ("whisper-1", "gpt-4o-transcribe", "gpt-4o-transcribe-diarize",
                         "gpt-4o-mini-transcribe-2025-12-15", " gpt-4o-transcribe",
                         "gpt-realtime", "gpt-realtime-2025-08-28", "gpt-4o-mini-realtime-preview"):
            with self.subTest(model_id=model_id), self.assertRaises(RegistryError):
                self.registry.add(base.evolve(id=uuid4(), model_id=model_id))
        with self.assertRaises(RegistryError):
            self.registry.update(base.id, {"mode": Mode.FILE})
        self.assertEqual(self.registry.get(base.id), base)

    def test_new_id_requires_explicit_compatible_profile(self):
        base = self.registry.list(Mode.FILE)[0]
        candidate = base.evolve(id=uuid4(), model_id="future-user-selected-model", display_name="Eksperiments")
        self.assertEqual(self.registry.add(candidate), candidate)
        with self.assertRaises(RegistryError):
            self.registry.add(candidate.evolve(id=uuid4(), capability_profile_id="missing"))

    def test_invalid_settings_rollback_without_mutation(self):
        whisper = next(m for m in self.registry.list(Mode.LIVE) if m.model_id == "gpt-realtime-whisper")
        with self.assertRaises(RegistryError):
            self.registry.update(whisper.id, {"settings": TranscriptionSettings(prompt="neatbalstīts")})
        self.assertEqual(self.registry.get(whisper.id), whisper)
        live = next(m for m in self.registry.list(Mode.LIVE) if m.model_id == "gpt-live-transcribe")
        with self.assertRaises(RegistryError):
            self.registry.update(live.id, {"settings": TranscriptionSettings(latency="low")})

    def test_concurrent_initialization_seeds_once(self):
        database = self.root / "concurrent.sqlite3"
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(ModelRegistry(database).initialize) for _ in range(3)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(len(ModelRegistry(database).list()), 11)

    def test_api_crud_snapshots_and_restart(self):
        settings = Settings(self.root)
        with TestClient(create_app(settings)) as client:
            original = client.get("/api/models?mode=file").json()[0]
            payload = {k: v for k, v in original.items() if k != "id"}
            payload["display_name"] = "Mans variants"
            added = client.post("/api/models", json=payload)
            self.assertEqual(added.status_code, 201)
            model = added.json()
            url = f"/api/models/{model['id']}"
            self.assertEqual(client.patch(url, json={"enabled": False}).status_code, 200)
            run_response = client.post("/api/runs", json={"mode": "file"})
            self.assertEqual(run_response.status_code, 201)
            run = run_response.json()
            self.assertNotIn(model["id"], {m["entry_id"] for m in run["models"]})
            self.assertEqual(run["status"], "queued")
            self.assertEqual(client.delete(url).status_code, 204)
            self.assertEqual(client.delete(url).status_code, 404)
        with TestClient(create_app(settings)) as restarted:
            self.assertEqual(restarted.get(url).status_code, 404)
            self.assertEqual(restarted.get(f"/api/runs/{run['id']}").json(), run)
            self.assertEqual(len(restarted.get("/api/capabilities").json()), 11)

    def test_api_validation_does_not_echo_secrets_or_accept_nulls(self):
        with TestClient(create_app(Settings(self.root))) as client:
            model = client.get("/api/models").json()[0]
            url = f"/api/models/{model['id']}"
            for payload in ({"api_key": "synthetic-secret"}, {"settings": {"api_key": "synthetic-secret"}},
                            {"enabled": None}, {"display_name": ""}, {"id": str(uuid4())}):
                response = client.patch(url, json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("synthetic-secret", response.text)
                self.assertEqual(client.get(url).json(), model)
            self.assertEqual(client.get("/api/models?mode=invalid").status_code, 422)
            self.assertEqual(client.get("/api/models/not-a-uuid").status_code, 422)
            self.assertEqual(client.get(f"/api/runs/{uuid4()}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
