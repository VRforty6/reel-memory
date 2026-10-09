from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import settings

router = APIRouter()


def _configured_apk() -> Path:
    raw = (settings.update_apk_path or "").strip()
    if not raw:
        raise HTTPException(status_code=503, detail="app updates are not configured")
    path = Path(raw).expanduser().resolve()
    if path.suffix.lower() != ".apk" or not path.is_file():
        raise HTTPException(status_code=503, detail="configured update APK is unavailable")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@router.get("/v1/app/update")
def get_app_update(
    current_version_code: int = Query(ge=1),
) -> dict:
    apk = _configured_apk()
    latest = settings.update_version_code
    if latest < 1 or not settings.update_version_name.strip():
        raise HTTPException(status_code=503, detail="app update version is not configured")
    return {
        "update_available": latest > current_version_code,
        "latest_version_code": latest,
        "latest_version_name": settings.update_version_name.strip(),
        "release_notes": settings.update_release_notes.strip() or None,
        "size_bytes": apk.stat().st_size,
        "sha256": _sha256(apk),
        "download_path": "/v1/app/update/apk",
    }


@router.get("/v1/app-update/latest")
def get_legacy_app_update(request: Request) -> dict:
    """Compatibility with pre-0.2.6 Android builds.

    Older APKs call this fixed route with no query string and compare the
    returned version code locally. Keep both snake_case and camelCase aliases
    until all installed clients have moved to the new /v1/app/update contract.
    """
    apk = _configured_apk()
    latest = settings.update_version_code
    name = settings.update_version_name.strip()
    if latest < 1 or not name:
        raise HTTPException(status_code=503, detail="app update version is not configured")
    digest = _sha256(apk)
    size = apk.stat().st_size
    download_path = "/v1/app-update/apk"
    legacy_apk_path = f"/downloads/reel-memory-{name}.apk"
    absolute_download = str(request.base_url).rstrip("/") + legacy_apk_path
    notes = settings.update_release_notes.strip() or "Reel Memory update"
    return {
        "version_code": latest,
        "version_name": name,
        "latest_version_code": latest,
        "latest_version_name": name,
        "versionCode": latest,
        "versionName": name,
        "latestVersionCode": latest,
        "latestVersionName": name,
        "release_notes": notes,
        "releaseNotes": notes,
        "notes": notes,
        "size_bytes": size,
        "sizeBytes": size,
        "sha256": digest,
        "download_path": download_path,
        "downloadPath": download_path,
        "apk_url": absolute_download,
        "apkUrl": absolute_download,
        "download_url": absolute_download,
        "downloadUrl": absolute_download,
        "url": absolute_download,
        "update_available": True,
        "updateAvailable": True,
        "isUpdateAvailable": True,
        "required": False,
        "mandatory": False,
        "force_update": False,
        "forceUpdate": False,
    }


def _download_response() -> FileResponse:
    apk = _configured_apk()
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename=f"reel-memory-{settings.update_version_name or 'update'}.apk",
    )


@router.get("/v1/app/update/apk")
def download_app_update() -> FileResponse:
    return _download_response()


@router.get("/v1/app-update/apk")
def download_legacy_app_update() -> FileResponse:
    return _download_response()


@router.get("/v1/app-update/download")
def download_legacy_app_update_alias() -> FileResponse:
    return _download_response()


@router.get("/downloads/reel-memory-{version_name}.apk")
def download_legacy_named_apk(version_name: str) -> FileResponse:
    """Serve immutable versioned APKs that still exist in the update archive.

    This keeps previously issued download links working even after a newer
    release becomes current. Only simple numeric semantic versions are accepted.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version_name):
        raise HTTPException(status_code=404, detail="update not found")
    archive_dir = _configured_apk().parent
    candidate = (archive_dir / f"reel-memory-{version_name}.apk").resolve()
    if candidate.parent != archive_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="update not found")
    return FileResponse(
        candidate,
        media_type="application/vnd.android.package-archive",
        filename=candidate.name,
    )
