"""Offline foundation checks using isolated storage and synthetic environment files."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings, load_settings
from app.main import create_app


class FoundationTests(unittest.TestCase):
    def test_startup_and_health_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            settings = load_settings(env_file=None)
            self.assertEqual(settings.storage_root, PROJECT_ROOT)
            app = create_app(Settings(Path(directory)))
            with TestClient(app) as client:
                response = client.get("/api/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok", "version": "0.1.0"})
                self.assertTrue(all((Path(directory) / name).is_dir()
                                    for name in ("data", "runs", "evaluations")))
                self.assertNotIn("OPENAI_API_KEY", response.text)

    def test_environment_precedence_and_read_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "test.env"
            # Synthetic values only; never open the project's credential file in tests.
            content = "OPENAI_API_KEY=synthetic-file-value\nP7_STORAGE_ROOT=from-file\n"
            env_file.write_text(content, encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-shell-value",
                                        "P7_STORAGE_ROOT": directory}, clear=True):
                settings = load_settings(env_file=env_file)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "synthetic-shell-value")
                self.assertEqual(settings.storage_root, Path(directory).resolve())
                self.assertNotIn("synthetic", repr(settings))
            self.assertEqual(env_file.read_text(encoding="utf-8"), content)

    def test_relative_storage_and_optional_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "test.env"
            env_file.write_text("P7_STORAGE_ROOT=alternate-output\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(env_file=env_file)
                self.assertEqual(settings.storage_root, PROJECT_ROOT / "alternate-output")
                defaults = load_settings(env_file=Path(directory) / "absent.env")
                # Absent file does not erase already loaded environment values.
                self.assertEqual(defaults.storage_root, settings.storage_root)

    def test_static_frontend_and_storage_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(Settings(root))) as client:
                (root / "runs" / "private.txt").write_text("synthetic-private", encoding="utf-8")
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn('<html lang="lv">', page.text)
                self.assertIn("Tiešraide", page.text)
                for asset in ("styles.css", "app.js"):
                    self.assertEqual(client.get(f"/static/{asset}").status_code, 200)
                for url in ("/.env", "/runs/private.txt", "/static/../.env", "/docs"):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("synthetic-private", response.text)

    def test_errors_are_localized(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(Settings(Path(directory)))) as client:
                self.assertEqual(client.get("/missing").json(), {"detail": "Lapa nav atrasta."})
                response = client.post("/api/health")
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.json(), {"detail": "Šī darbība nav pieejama."})


if __name__ == "__main__":
    unittest.main()
