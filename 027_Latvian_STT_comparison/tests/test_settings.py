"""Language mapping and capability validation for the settings UI."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.contracts import Mode, Provider, TranscriptionSettings
from app.main import create_app
from app.providers.settings import effective_settings, resolve_language
from app.registry import ModelRegistry, RegistryError


class SettingsTests(unittest.TestCase):
    def test_latvian_codes_and_auto_detection(self):
        for provider in Provider:
            expected = "lv-LV" if provider == Provider.GEMINI else "lv"
            for hint in ("lv", "lv-LV", "LV-lv"):
                self.assertEqual(resolve_language(provider, hint), expected)
            self.assertIsNone(resolve_language(provider, None))
        self.assertEqual(resolve_language(Provider.GEMINI, "en"), "en-US")
        self.assertEqual(resolve_language(Provider.GEMINI, "en-gb"), "en-GB")

    def test_ambiguous_or_invalid_codes_not_invented(self):
        for hint in ("lav", "", "1v"):
            with self.assertRaises(ValueError): resolve_language(Provider.OPENAI, hint)
        with self.assertRaises(ValueError): resolve_language(Provider.GEMINI, "aa")

    def test_resolution_keeps_requested_settings_unchanged(self):
        requested = TranscriptionSettings(language_hint="lv-LV", keywords=("Latvija",))
        effective = effective_settings(Provider.OPENAI, requested)
        self.assertEqual(requested.language_hint, "lv-LV")
        self.assertEqual(effective.language_hint, "lv")
        self.assertEqual(effective.keywords, requested.keywords)

    def test_preview_is_offline_and_settings_validation_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(Settings(root))) as client:
                preview = client.post("/api/language-preview", json={"provider": "gemini", "language_hint": "lv"})
                self.assertEqual(preview.json(), {"language_code": "lv-LV"})
                self.assertEqual(client.post("/api/language-preview", json={"provider": "openai", "language_hint": "lav"}).status_code, 422)
                live = client.get("/api/models?mode=live").json()
                whisper = next(m for m in live if m["model_id"] == "gpt-realtime-whisper")
                url = f"/api/models/{whisper['id']}"
                response = client.patch(url, json={"settings": {"language_hint": "lv", "prompt": "neatbalstīts"}})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(client.get(url).json(), whisper)
                saved = client.patch(url, json={"settings": {"language_hint": "lv", "latency": "high"}})
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.json()["settings"]["latency"], "high")

    def test_vocabulary_limits_and_file_turn_control(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ModelRegistry(Path(directory) / "app.sqlite3")
            registry.initialize()
            eleven = next(m for m in registry.list(Mode.LIVE) if m.provider == Provider.ELEVENLABS)
            for keywords in (("x" * 21,), tuple(str(i) for i in range(51))):
                with self.assertRaises(RegistryError):
                    registry.update(eleven.id, {"settings": TranscriptionSettings(keywords=keywords)})
            file_model = registry.list(Mode.FILE)[0]
            with self.assertRaises(RegistryError):
                registry.update(file_model.id, {"settings": TranscriptionSettings(turn_detection="manual")})


if __name__ == "__main__":
    unittest.main()
