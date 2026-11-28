"""Unit tests for scripts/updater.py."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.updater import (
    _version_tuple,
    check_github_update,
    detect_system_proxy,
    perform_update,
)


class TestUpdater(unittest.TestCase):
    def test_version_tuple(self):
        self.assertEqual(_version_tuple("v0.4.3"), (0, 4, 3))
        self.assertEqual(_version_tuple("0.4.1"), (0, 4, 1))
        self.assertTrue(_version_tuple("v0.4.3") > _version_tuple("v0.4.2"))
        self.assertTrue(_version_tuple("1.0.0") > _version_tuple("0.9.9"))

    def test_detect_proxy_env(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:8888"}):
            self.assertEqual(detect_system_proxy(), "http://127.0.0.1:8888")

    def test_perform_update_preserves_config(self):
        """Verify that updating into a folder preserves existing config.json and state.db."""
        temp_dir = Path(tempfile.mkdtemp(prefix="test_updater_target_"))
        try:
            # Create dummy config and database
            config_file = temp_dir / "config.json"
            config_file.write_text(json.dumps({"my_token": "secret123"}), encoding="utf-8")
            db_file = temp_dir / "state.db"
            db_file.write_text("sqlite db content", encoding="utf-8")

            # Mock check_github_update and download_file
            fake_info = {
                "latest_version": "0.4.4",
                "zip_url": "https://fake/update.zip",
            }

            def fake_download(url, target_zip, timeout=60, log_fn=None):
                import zipfile
                with zipfile.ZipFile(target_zip, "w") as zf:
                    # In zip, include a new code file and a default config.json
                    zf.writestr("repo-0.4.4/agsync/new_module.py", "# new code")
                    zf.writestr("repo-0.4.4/config.json", json.dumps({"default": True}))
                    zf.writestr("repo-0.4.4/state.db", "OVERWRITTEN DB")
                return True

            with patch("scripts.updater.check_github_update", return_value=fake_info), \
                 patch("scripts.updater.download_file", side_effect=fake_download):
                ok, msg = perform_update(root_dir=temp_dir)
                self.assertTrue(ok)
                self.assertIn("0.4.4", msg)

            # Check that new code file was copied
            self.assertTrue((temp_dir / "agsync" / "new_module.py").exists())

            # Check that user's config.json and state.db were NOT overwritten
            saved_config = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_config.get("my_token"), "secret123")
            self.assertEqual(db_file.read_text(encoding="utf-8"), "sqlite db content")

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
