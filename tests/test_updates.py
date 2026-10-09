from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.updates import download_app_update, download_legacy_named_apk, get_app_update
from app.config import settings


def test_update_metadata_and_file_response(monkeypatch, tmp_path: Path):
    apk = tmp_path / "reel-memory.apk"
    apk.write_bytes(b"signed-apk-fixture")
    monkeypatch.setattr(settings, "update_apk_path", str(apk))
    monkeypatch.setattr(settings, "update_version_code", 3)
    monkeypatch.setattr(settings, "update_version_name", "0.1.2")
    monkeypatch.setattr(settings, "update_release_notes", "Category tree")

    payload = get_app_update(current_version_code=2)
    assert payload["update_available"] is True
    assert payload["latest_version_code"] == 3
    assert payload["latest_version_name"] == "0.1.2"
    assert payload["size_bytes"] == apk.stat().st_size
    assert len(payload["sha256"]) == 64
    assert payload["download_path"] == "/v1/app/update/apk"

    response = download_app_update()
    assert Path(response.path) == apk.resolve()
    assert response.media_type == "application/vnd.android.package-archive"


def test_update_reports_current_version(monkeypatch, tmp_path: Path):
    apk = tmp_path / "reel-memory.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(settings, "update_apk_path", str(apk))
    monkeypatch.setattr(settings, "update_version_code", 3)
    monkeypatch.setattr(settings, "update_version_name", "0.1.2")
    assert get_app_update(current_version_code=3)["update_available"] is False


def test_update_rejects_missing_apk(monkeypatch):
    monkeypatch.setattr(settings, "update_apk_path", None)
    with pytest.raises(HTTPException) as exc:
        get_app_update(current_version_code=1)
    assert exc.value.status_code == 503


def test_versioned_update_links_remain_valid_after_new_release(monkeypatch, tmp_path: Path):
    old_apk = tmp_path / "reel-memory-0.2.8.apk"
    old_apk.write_bytes(b"old-apk")
    current_apk = tmp_path / "reel-memory-0.2.9.apk"
    current_apk.write_bytes(b"new-apk")
    monkeypatch.setattr(settings, "update_apk_path", str(current_apk))
    monkeypatch.setattr(settings, "update_version_name", "0.2.9")

    response = download_legacy_named_apk("0.2.8")
    assert Path(response.path) == old_apk.resolve()
    assert response.filename == "reel-memory-0.2.8.apk"


def test_versioned_update_link_rejects_invalid_or_missing_version(monkeypatch, tmp_path: Path):
    current_apk = tmp_path / "reel-memory-0.2.9.apk"
    current_apk.write_bytes(b"new-apk")
    monkeypatch.setattr(settings, "update_apk_path", str(current_apk))
    monkeypatch.setattr(settings, "update_version_name", "0.2.9")

    for version in ("0.2.10", "../secret", "latest"):
        with pytest.raises(HTTPException) as exc:
            download_legacy_named_apk(version)
        assert exc.value.status_code == 404