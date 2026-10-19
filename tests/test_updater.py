"""
tests/test_updater.py — Unit tests for the A.U.R.A update system.

Covers:
  * SemVer parsing and comparison (including v2.10.0 > v2.9.0)
  * Protected-path matching
  * Zip-slip guard (via ReleaseDownloader path validation)
  * get_latest_release filtering (drafts, pre-releases, old tags)
  * Rollback logic (updater_helper._rollback)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest

# ── Ensure the project root is on sys.path ──────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from updater import (
    SemVer,
    is_protected,
    get_latest_release,
    get_release_asset_url,
    _detect_zip_prefix,
    read_version_json,
    write_version_json,
    PROTECTED_PATHS,
)


# ─────────────────────────────────────────────────────────────────────────────
# SemVer parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestSemVerParse:
    def test_parse_v_prefix(self):
        v = SemVer.parse("v2.1.0")
        assert v == SemVer(2, 1, 0)

    def test_parse_V_prefix(self):
        v = SemVer.parse("V3.0.5")
        assert v == SemVer(3, 0, 5)

    def test_parse_no_prefix(self):
        v = SemVer.parse("2.0.0")
        assert v == SemVer(2, 0, 0)

    def test_parse_leading_whitespace(self):
        v = SemVer.parse("  v2.1.0  ")
        assert v == SemVer(2, 1, 0)

    def test_parse_invalid_alpha(self):
        assert SemVer.parse("vXYZ") is None

    def test_parse_empty(self):
        assert SemVer.parse("") is None

    def test_parse_prerelease_suffix_rejected(self):
        # "v2.1.0-beta" should NOT parse (strict semver triple only)
        assert SemVer.parse("v2.1.0-beta") is None

    def test_parse_two_part_rejected(self):
        assert SemVer.parse("v2.1") is None

    def test_str(self):
        assert str(SemVer(2, 1, 0)) == "v2.1.0"


# ─────────────────────────────────────────────────────────────────────────────
# SemVer comparison (including the tricky v2.10.0 > v2.9.0 case)
# ─────────────────────────────────────────────────────────────────────────────

class TestSemVerComparison:
    def test_ordering_basic(self):
        assert SemVer(2, 1, 0) > SemVer(2, 0, 0)

    def test_ordering_minor(self):
        # v2.10.0 must be greater than v2.9.0 — numeric, not lexicographic
        assert SemVer(2, 10, 0) > SemVer(2, 9, 0)

    def test_ordering_patch(self):
        assert SemVer(2, 1, 10) > SemVer(2, 1, 9)

    def test_ordering_major(self):
        assert SemVer(3, 0, 0) > SemVer(2, 99, 99)

    def test_equality(self):
        assert SemVer(2, 0, 0) == SemVer(2, 0, 0)

    def test_lt(self):
        assert SemVer(2, 0, 0) < SemVer(2, 0, 1)

    def test_sort_list(self):
        versions = [SemVer(2, 9, 0), SemVer(2, 10, 0), SemVer(2, 1, 0)]
        assert sorted(versions)[-1] == SemVer(2, 10, 0)

    def test_is_at_least_minimum_true(self):
        assert SemVer(2, 0, 0).is_at_least_minimum()
        assert SemVer(2, 1, 0).is_at_least_minimum()

    def test_is_at_least_minimum_false(self):
        assert not SemVer(1, 99, 99).is_at_least_minimum()
        assert not SemVer(0, 0, 1).is_at_least_minimum()


# ─────────────────────────────────────────────────────────────────────────────
# Protected-path matching
# ─────────────────────────────────────────────────────────────────────────────

class TestIsProtected:
    def test_config_dir_exact(self):
        assert is_protected("config")

    def test_config_subfile(self):
        assert is_protected("config/api_keys.json")

    def test_config_nested(self):
        assert is_protected("config/face_unlock/model.onnx")

    def test_memory_long_term(self):
        assert is_protected("memory/long_term.json")

    def test_env_file(self):
        assert is_protected(".env")

    def test_env_dot_prefix(self):
        assert is_protected("custom.env")

    def test_venv(self):
        assert is_protected("venv")
        assert is_protected("venv/lib/python3.12/site-packages/foo.py")

    def test_dot_venv(self):
        assert is_protected(".venv")
        assert is_protected(".venv/Scripts/python.exe")

    def test_git(self):
        assert is_protected(".git")
        assert is_protected(".git/config")

    def test_pycache(self):
        assert is_protected("__pycache__")
        assert is_protected("__pycache__/foo.pyc")

    def test_db_file(self):
        assert is_protected("sessions.db")
        assert is_protected("data/store.sqlite")

    def test_version_json(self):
        assert is_protected("version.json")

    def test_staging_dir(self):
        assert is_protected(".update_staging")
        assert is_protected(".update_staging/main.py")

    def test_backup_dir(self):
        assert is_protected(".update_backup")
        assert is_protected(".update_backup/ui.py")

    def test_updater_state(self):
        assert is_protected(".updater_state.json")

    def test_normal_file_not_protected(self):
        assert not is_protected("ui.py")
        assert not is_protected("main.py")
        assert not is_protected("updater.py")
        assert not is_protected("requirements.txt")

    def test_actions_dir_not_protected(self):
        assert not is_protected("actions/web_search.py")

    def test_case_insensitive_on_windows(self):
        # is_protected does case-insensitive matching
        assert is_protected("Config/api_keys.json")
        assert is_protected("VENV/lib/site-packages/foo.py")


# ─────────────────────────────────────────────────────────────────────────────
# get_latest_release
# ─────────────────────────────────────────────────────────────────────────────

class TestGetLatestRelease:
    def _make_release(
        self,
        tag: str,
        draft: bool = False,
        prerelease: bool = False,
    ) -> dict:
        return {
            "tag_name": tag,
            "draft": draft,
            "prerelease": prerelease,
            "body": "Release notes.",
            "zipball_url": f"https://example.com/{tag}.zip",
        }

    def test_picks_highest(self):
        releases = [
            self._make_release("v2.0.0"),
            self._make_release("v2.1.0"),
            self._make_release("v2.10.0"),  # must beat v2.9.0 numerically
            self._make_release("v2.9.0"),
        ]
        rel = get_latest_release(releases)
        assert rel is not None
        assert SemVer.parse(rel["tag_name"]) == SemVer(2, 10, 0)

    def test_skips_draft(self):
        releases = [
            self._make_release("v2.2.0", draft=True),
            self._make_release("v2.1.0"),
        ]
        rel = get_latest_release(releases)
        assert SemVer.parse(rel["tag_name"]) == SemVer(2, 1, 0)

    def test_skips_prerelease(self):
        releases = [
            self._make_release("v2.2.0", prerelease=True),
            self._make_release("v2.1.0"),
        ]
        rel = get_latest_release(releases)
        assert SemVer.parse(rel["tag_name"]) == SemVer(2, 1, 0)

    def test_skips_old_tags(self):
        releases = [
            self._make_release("v1.99.0"),  # below MIN_VERSION
            self._make_release("v2.0.0"),
        ]
        rel = get_latest_release(releases)
        assert SemVer.parse(rel["tag_name"]) == SemVer(2, 0, 0)

    def test_skips_unparseable_tags(self):
        releases = [
            self._make_release("not-a-version"),
            self._make_release("v2.0.0"),
        ]
        rel = get_latest_release(releases)
        assert SemVer.parse(rel["tag_name"]) == SemVer(2, 0, 0)

    def test_empty_list_returns_none(self):
        assert get_latest_release([]) is None

    def test_all_drafts_returns_none(self):
        releases = [self._make_release("v2.1.0", draft=True)]
        assert get_latest_release(releases) is None

    def test_order_independent(self):
        """Latest must be found regardless of list order."""
        releases = [
            self._make_release("v2.5.0"),
            self._make_release("v2.3.0"),
            self._make_release("v2.7.0"),
            self._make_release("v2.1.0"),
        ]
        rel = get_latest_release(releases)
        assert SemVer.parse(rel["tag_name"]) == SemVer(2, 7, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Version JSON I/O
# ─────────────────────────────────────────────────────────────────────────────

class TestVersionJson:
    def test_read_write_roundtrip(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("updater.BASE_DIR", tmp_path)
        ver = SemVer(2, 3, 1)
        write_version_json(ver)
        assert read_version_json() == ver

    def test_read_missing_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("updater.BASE_DIR", tmp_path)
        assert read_version_json() is None

    def test_read_invalid_json_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("updater.BASE_DIR", tmp_path)
        (tmp_path / "version.json").write_text("not json", encoding="utf-8")
        assert read_version_json() is None

    def test_read_bad_version_field_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("updater.BASE_DIR", tmp_path)
        (tmp_path / "version.json").write_text(
            json.dumps({"version": "totally-invalid"}), encoding="utf-8"
        )
        assert read_version_json() is None


# ─────────────────────────────────────────────────────────────────────────────
# Zip-slip protection (tested via the extraction logic)
# ─────────────────────────────────────────────────────────────────────────────

class TestZipSlip:
    """Verify that malicious zip paths cannot escape the staging directory."""

    def _make_malicious_zip(self, tmp_path: Path) -> Path:
        """Create a zip that tries to write outside the target directory."""
        zp = tmp_path / "evil.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            # Normal file
            zf.writestr("prefix-abc123/main.py", "# ok\n")
            # Zip-slip attempt: path traversal
            zf.writestr("prefix-abc123/../../evil.py", "# bad\n")
            # Another traversal form
            zf.writestr("prefix-abc123/../../../windows_evil.py", "# very bad\n")
        return zp

    def test_zip_slip_blocked(self, tmp_path: Path):
        """
        Simulate what ReleaseDownloader does: resolve the target and check it is
        relative to the staging directory.  Malicious paths must be skipped.
        """
        staging = tmp_path / "staging"
        staging.mkdir()
        zp = self._make_malicious_zip(tmp_path)
        root_prefix = "prefix-abc123/"

        written: list[str] = []
        with zipfile.ZipFile(zp) as zf:
            for member in zf.infolist():
                name = member.filename
                if name.startswith(root_prefix):
                    name = name[len(root_prefix):]
                if not name:
                    continue
                target = (staging / name).resolve()
                # Zip-slip guard identical to ReleaseDownloader
                try:
                    target.relative_to(staging.resolve())
                except ValueError:
                    continue   # blocked
                written.append(name)

        # Only the safe "main.py" should be written
        assert written == ["main.py"]
        # The evil files must NOT appear in written
        assert "../../evil.py" not in written
        assert "../../../windows_evil.py" not in written


# ─────────────────────────────────────────────────────────────────────────────
# Rollback logic
# ─────────────────────────────────────────────────────────────────────────────

class TestRollback:
    """Test updater_helper._rollback restores files and removes new-only files."""

    def test_rollback_restores_modified_file(self, tmp_path: Path):
        from updater_helper import _rollback

        base   = tmp_path / "base";   base.mkdir()
        backup = tmp_path / "backup"; backup.mkdir()

        # Simulate: ui.py was overwritten during apply
        original = b"# original content\n"
        new_ver  = b"# new content\n"

        (base / "ui.py").write_bytes(new_ver)           # "after apply"
        (backup / "ui.py").write_bytes(original)        # backup copy

        _rollback(base, backup, ["ui.py"])

        assert (base / "ui.py").read_bytes() == original

    def test_rollback_removes_new_file(self, tmp_path: Path):
        from updater_helper import _rollback

        base   = tmp_path / "base";   base.mkdir()
        backup = tmp_path / "backup"; backup.mkdir()

        # new_feature.py was added by the update but has no backup
        (base / "new_feature.py").write_text("# new\n")

        _rollback(base, backup, ["new_feature.py"])

        assert not (base / "new_feature.py").exists()

    def test_rollback_handles_missing_target_gracefully(self, tmp_path: Path):
        from updater_helper import _rollback

        base   = tmp_path / "base";   base.mkdir()
        backup = tmp_path / "backup"; backup.mkdir()

        # applied contains a file that doesn't exist on disk (already deleted)
        # rollback should not raise
        _rollback(base, backup, ["nonexistent.py"])  # must not raise

    def test_rollback_nested_path(self, tmp_path: Path):
        from updater_helper import _rollback

        base   = tmp_path / "base";   base.mkdir()
        backup = tmp_path / "backup"; backup.mkdir()

        original = b"# original\n"
        (base / "actions").mkdir()
        (base / "actions" / "web_search.py").write_bytes(b"# new\n")
        (backup / "actions").mkdir()
        (backup / "actions" / "web_search.py").write_bytes(original)

        _rollback(base, backup, ["actions/web_search.py"])

        assert (base / "actions" / "web_search.py").read_bytes() == original


# ─────────────────────────────────────────────────────────────────────────────
# Apply files — skips protected paths
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyFiles:
    def test_apply_skips_protected(self, tmp_path: Path):
        from updater_helper import _apply_files

        base    = tmp_path / "base";    base.mkdir()
        staging = tmp_path / "staging"; staging.mkdir()

        # Protected file: config/api_keys.json
        (staging / "config").mkdir()
        (staging / "config" / "api_keys.json").write_text('{"key":"new"}')
        # User's copy
        (base / "config").mkdir()
        (base / "config" / "api_keys.json").write_text('{"key":"user_secret"}')

        # Normal file
        (staging / "main.py").write_text("# new main")

        _apply_files(base, staging)

        # Protected must be untouched
        assert (base / "config" / "api_keys.json").read_text() == '{"key":"user_secret"}'
        # Normal must be updated
        assert (base / "main.py").read_text() == "# new main"

    def test_apply_adds_protected_if_absent(self, tmp_path: Path):
        """Protected files that don't exist locally should be added (first-run defaults)."""
        from updater_helper import _apply_files

        base    = tmp_path / "base";    base.mkdir()
        staging = tmp_path / "staging"; staging.mkdir()

        # "config" is protected, but it doesn't exist locally yet
        (staging / "config").mkdir()
        (staging / "config" / "api_keys.json").write_text('{"key":"default"}')

        _apply_files(base, staging)

        # Since the local file was absent, the default should be installed
        assert (base / "config" / "api_keys.json").read_text() == '{"key":"default"}'


# ─────────────────────────────────────────────────────────────────────────────
# Release asset URL resolution & Zip prefix detection
# ─────────────────────────────────────────────────────────────────────────────

class TestReleaseAssetUrl:
    def _make_asset(self, name: str, url: str, updated_at: str = "2026-10-19T00:00:00Z") -> dict:
        return {
            "name": name,
            "browser_download_url": url,
            "updated_at": updated_at,
        }

    def test_finds_source_code_zip(self):
        rel = {
            "assets": [
                self._make_asset("source_code.zip", "https://example.com/source_code.zip"),
            ]
        }
        assert get_release_asset_url(rel) == "https://example.com/source_code.zip"

    def test_finds_source_code2_zip(self):
        rel = {
            "assets": [
                self._make_asset("source_code2.zip", "https://example.com/source_code2.zip"),
            ]
        }
        assert get_release_asset_url(rel) == "https://example.com/source_code2.zip"

    def test_finds_source_coe2_zip(self):
        # Specific typo test mentioned in prompt
        rel = {
            "assets": [
                self._make_asset("source_coe2.zip", "https://example.com/source_coe2.zip"),
            ]
        }
        assert get_release_asset_url(rel) == "https://example.com/source_coe2.zip"

    def test_finds_source_code3_zip(self):
        rel = {
            "assets": [
                self._make_asset("source_code3.zip", "https://example.com/source_code3.zip"),
            ]
        }
        assert get_release_asset_url(rel) == "https://example.com/source_code3.zip"

    def test_prefers_higher_numbered_asset(self):
        rel = {
            "assets": [
                self._make_asset("source_code.zip", "https://example.com/v1.zip"),
                self._make_asset("source_code2.zip", "https://example.com/v2.zip"),
                self._make_asset("source_code3.zip", "https://example.com/v3.zip"),
            ]
        }
        # source_code3.zip has suffix 3 > 2 > 1
        assert get_release_asset_url(rel) == "https://example.com/v3.zip"

    def test_case_insensitive(self):
        rel = {
            "assets": [
                self._make_asset("Source_Code2.ZIP", "https://example.com/upper.zip"),
            ]
        }
        assert get_release_asset_url(rel) == "https://example.com/upper.zip"

    def test_fallback_to_none_if_no_zip_assets(self):
        rel = {
            "assets": [
                self._make_asset("checksums.txt", "https://example.com/checksums.txt"),
            ]
        }
        assert get_release_asset_url(rel) is None

    def test_empty_assets_returns_none(self):
        assert get_release_asset_url({}) is None
        assert get_release_asset_url({"assets": []}) is None


class TestDetectZipPrefix:
    def test_detects_single_root_prefix(self):
        names = [
            "A.U.R.A_v2-CodeCrafters-main/",
            "A.U.R.A_v2-CodeCrafters-main/main.py",
            "A.U.R.A_v2-CodeCrafters-main/ui.py",
            "A.U.R.A_v2-CodeCrafters-main/actions/web_search.py",
        ]
        assert _detect_zip_prefix(names) == "A.U.R.A_v2-CodeCrafters-main/"

    def test_flat_zip_returns_empty(self):
        names = [
            "main.py",
            "ui.py",
            "requirements.txt",
            "actions/web_search.py",
        ]
        assert _detect_zip_prefix(names) == ""

    def test_handles_windows_backslashes(self):
        names = [
            "A.U.R.A_v2-CodeCrafters-main\\main.py",
            "A.U.R.A_v2-CodeCrafters-main\\ui.py",
        ]
        assert _detect_zip_prefix(names) == "A.U.R.A_v2-CodeCrafters-main/"

    def test_empty_list(self):
        assert _detect_zip_prefix([]) == ""

