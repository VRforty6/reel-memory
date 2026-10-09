"""Capture API — PRD §39, §12 (capture-first), §13 (duplicates).

POST /v1/captures validates + canonicalizes the URL, get-or-creates the
source item, records the capture and (unless it's a READY duplicate) queues a
processing job — all durably committed BEFORE returning 202 (FR-CAP-004).
Duplicates of a READY memory return 200 with duplicate=true and run no AI.

POST /v1/captures/upload accepts a direct video or image file (the "see the
reel" path: unauthenticated Instagram acquisition yields 0% video bytes, see
MILESTONE2-BENCHMARK.md). The file is validated, stored as untrusted input
under the upload dir, deduplicated by content hash, and handed to the normal
worker pipeline via the "upload" source adapter.

POST /v1/captures/url ingests a web page (platform "web"): the worker fetches
it SSRF-safely and stores the extracted main article text as an "article"
memory segment.

POST /v1/captures/album accepts a carousel/album share: 2-30 files
(image/*, at most one video/*) that become ONE memory. Each photo is
processed (vision + OCR) and every segment carries its album_index so Q&A
citations can name the photo. An album counts as ONE capture against the
quota; dedup is by the combined content hash (order-independent).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from app.capture.canonicalize import CanonicalizationError, canonicalize_url
from app.capture.dedupe import should_reprocess
from app.config import settings
from app.auth import get_current_user
from app.db import get_db
from app.entitlements import BUCKET_CAPTURES, check_and_increment_quota
from app.models import Capture, Memory, ProcessingJob, SourceItem, User
from app.pipeline.state_machine import ProcessingStatus
from app.schemas import (
    AlbumCaptureResponse,
    CaptureRequest,
    CaptureResponse,
    UrlCaptureRequest,
)
from app.sources.webpage import canonicalize_web_url
from app.sources.youtube import canonicalize_youtube_url, is_youtube_url

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_or_create_item(
    db: Session,
    user: User,
    *,
    platform: str,
    platform_item_id: str,
    canonical_url: str,
    original_url: str,
    source_status: str,
) -> SourceItem:
    """Get-or-create a SourceItem; duplicate shares bump bookkeeping only
    (PRD §13: no new AI processing for a re-share)."""
    now = _utcnow()
    item = (
        db.query(SourceItem)
        .filter(
            SourceItem.user_id == user.id,
            SourceItem.platform == platform,
            SourceItem.platform_item_id == platform_item_id,
        )
        .first()
    )
    if item is None:
        item = SourceItem(
            user_id=user.id,
            platform=platform,
            platform_item_id=platform_item_id,
            canonical_url=canonical_url,
            original_url=original_url,
            source_status=source_status,
            first_saved_at=now,
            last_saved_at=now,
            save_count=1,
        )
        db.add(item)
        db.flush()
    else:
        item.save_count = (item.save_count or 0) + 1
        item.last_saved_at = now
    return item


def _record_capture(
    db: Session, user: User, item: SourceItem, capture_source: str
) -> tuple[CaptureResponse, bool]:
    """Shared capture-first flow: record the capture, get-or-create the memory,
    queue a processing job unless this is a READY duplicate. Commits before
    returning (FR-CAP-004). Returns (response, http_200_for_duplicate)."""
    capture = Capture(source_item_id=item.id, capture_source=capture_source)
    db.add(capture)
    db.flush()

    memory = db.query(Memory).filter(Memory.source_item_id == item.id).first()
    duplicate = False
    if memory is None:
        memory = Memory(
            source_item_id=item.id,
            processing_status=ProcessingStatus.CAPTURED.value,
            processing_version=settings.processing_version,
        )
        db.add(memory)
        db.flush()
        db.add(
            ProcessingJob(
                memory_id=memory.id,
                status="QUEUED",
                stage=ProcessingStatus.QUEUED.value,
                attempt_count=0,
            )
        )
    elif memory.processing_status == ProcessingStatus.READY.value and not should_reprocess(
        memory.processing_status, memory.processing_version, settings.processing_version
    ):
        duplicate = True
    else:
        # Existing memory that never finished: make sure a job is queued.
        latest = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.memory_id == memory.id)
            .order_by(ProcessingJob.id.desc())
            .first()
        )
        if latest is None or latest.status in ("DONE", "FAILED"):
            db.add(
                ProcessingJob(
                    memory_id=memory.id,
                    status="QUEUED",
                    stage=ProcessingStatus.QUEUED.value,
                    attempt_count=0,
                )
            )

    # Capture-first (§8.2/FR-CAP-004): everything is committed before we answer.
    db.commit()
    return (
        CaptureResponse(
            id=capture.id,
            memory_id=memory.id,
            status=memory.processing_status,
            duplicate=duplicate,
        ),
        duplicate,
    )


@router.post("/v1/captures", response_model=CaptureResponse)
def create_capture(
    req: CaptureRequest,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaptureResponse:
    try:
        canon = canonicalize_url(req.url)
    except CanonicalizationError as e:
        raise HTTPException(
            status_code=422, detail={"code": e.code, "message": str(e)}
        ) from e

    check_and_increment_quota(db, user, BUCKET_CAPTURES)

    item = _get_or_create_item(
        db,
        user,
        platform=canon.platform,
        platform_item_id=canon.platform_item_id,
        canonical_url=canon.canonical_url,
        original_url=canon.original_url,
        source_status="UNKNOWN",
    )

    resp, duplicate = _record_capture(db, user, item, capture_source="api")
    response.status_code = 200 if duplicate else 202
    return resp


# --- direct video/image upload ----------------------------------------------

# Conservative allowlist: content-type must be video/* or image/*; the stored
# extension is derived from this mapping, never from the client-supplied
# filename.
_UPLOAD_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/3gpp": ".3gp",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks


def sanitize_filename(name: str | None) -> str:
    """Strip path components and hostile characters from a client filename.
    Pure function (unit-testable)."""
    base = (name or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    safe = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip("._ ")
    return safe[:128] or "upload"


def validate_upload_content_type(content_type: str | None) -> str:
    """Return the normalized content-type or raise 415. Pure (unit-testable)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not (ct.startswith("video/") or ct.startswith("image/")):
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_MEDIA_TYPE",
                "message": f"expected a video/* or image/* upload, got {content_type!r}",
            },
        )
    return ct


def write_upload_stream(
    stream: BinaryIO, dest: Path, max_bytes: int
) -> str:
    """Stream `stream` to `dest` (never fully in memory), enforcing the size
    cap as bytes arrive. Returns the sha256 hex digest. On oversize, removes
    the partial file and raises 413. Pure-ish (unit-testable with BytesIO)."""
    digest = hashlib.sha256()
    total = 0
    with open(dest, "wb") as f:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail={
                        "code": "UPLOAD_TOO_LARGE",
                        "message": (
                            f"upload exceeds the {max_bytes // (1024 * 1024)} MiB cap "
                            f"(MAX_UPLOAD_MB)"
                        ),
                    },
                )
            digest.update(chunk)
            f.write(chunk)
    return digest.hexdigest()


@router.post("/v1/captures/upload", response_model=CaptureResponse)
async def upload_capture(
    response: Response,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaptureResponse:
    """Direct video/image upload — the "see the reel" path.

    Accepts video/* (reels) and image/* (screenshots, post images). Validates
    content-type and size (MAX_UPLOAD_MB), stores the file as untrusted input
    outside any web-served root, deduplicates by content hash, and enqueues
    the normal worker pipeline via the "upload" adapter. Images skip audio
    extraction — the image itself is the frame set for vision + OCR.
    Returns 202 (or 200 with duplicate=true for an already-READY upload).
    """
    # Quota first: an over-quota user must not be able to burn upload bytes.
    check_and_increment_quota(db, user, BUCKET_CAPTURES)
    content_type = validate_upload_content_type(file.content_type)
    safe_name = sanitize_filename(file.filename)
    max_bytes = settings.max_upload_mb * 1024 * 1024

    upload_dir = Path(settings.resolve_upload_dir())
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Stream to a temp part-file first so an oversize/aborted upload never
    # leaves a half-written final file behind.
    part = upload_dir / f".part-{uuid.uuid4().hex}"
    digest = write_upload_stream(file.file, part, max_bytes)
    final = upload_dir / f"{digest}{_UPLOAD_EXTENSIONS.get(content_type, '.mp4')}"
    try:
        # Atomic publish; a concurrent identical upload simply wins the race —
        # same bytes, same hash, harmless.
        part.replace(final)
    except OSError:
        if final.exists():
            part.unlink(missing_ok=True)  # lost the race; identical bytes stored
        else:
            part.unlink(missing_ok=True)
            raise

    item = _get_or_create_item(
        db,
        user,
        platform="upload",
        platform_item_id=digest,
        canonical_url=f"upload://{digest}",
        original_url=safe_name,
        source_status="UPLOADED",
    )

    resp, duplicate = _record_capture(db, user, item, capture_source="upload")
    response.status_code = 200 if duplicate else 202
    return resp


# --- website ingestion -------------------------------------------------------


@router.post("/v1/captures/url", response_model=CaptureResponse)
def capture_url(
    req: UrlCaptureRequest,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CaptureResponse:
    """Ingest a web page or YouTube video URL as a memory.

    YouTube video/Short URLs are routed to the dedicated YouTube source
    adapter (public metadata + existing subtitles). Other URLs stay on the
    SSRF-safe article fetch path. Fetch failures are classified honestly by the worker
    (retryable / auth-required / gone / unsupported).
    Returns 202 (or 200 with duplicate=true for an already-READY page).
    """
    try:
        canon = (
            canonicalize_youtube_url(req.url)
            if is_youtube_url(req.url)
            else canonicalize_web_url(req.url)
        )
    except CanonicalizationError as e:
        raise HTTPException(
            status_code=422, detail={"code": e.code, "message": str(e)}
        ) from e

    check_and_increment_quota(db, user, BUCKET_CAPTURES)

    item = _get_or_create_item(
        db,
        user,
        platform=canon.platform,
        platform_item_id=canon.platform_item_id,
        canonical_url=canon.canonical_url,
        original_url=canon.original_url,
        source_status="UNKNOWN",
    )

    resp, duplicate = _record_capture(db, user, item, capture_source="url")
    response.status_code = 200 if duplicate else 202
    return resp


# --- album / carousel capture ------------------------------------------------


def album_combined_hash(hashes: list[str]) -> str:
    """Order-independent album identity: sha256 of the ':'-joined SORTED
    per-file hashes. The same photos shared in a different order dedup to
    the same memory. Pure (unit-testable)."""
    return hashlib.sha256(":".join(sorted(hashes)).encode("utf-8")).hexdigest()


def album_file_name(combined: str, index: int, file_hash: str, ext: str) -> str:
    """Deterministic per-photo storage name inside the upload dir. Pure.

    Layout <combined>.<index:02d>.<file_sha256>.<ext> lets the upload
    adapter find the whole album with one glob and order the photos.
    """
    return f"{combined}.{index:02d}.{file_hash}{ext}"


def validate_album_files(files: list[UploadFile]) -> None:
    """Cheap header-based validation of the album file list. Raises 422.

    Authoritative per-file checks (content-type allowlist, size caps) happen
    while streaming; the video count is re-checked there too since headers
    are client-supplied.
    """
    n = len(files)
    if n < settings.min_album_files or n > settings.max_album_files:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ALBUM_FILE_COUNT",
                "message": (
                    f"an album needs {settings.min_album_files}-"
                    f"{settings.max_album_files} files, got {n}"
                ),
            },
        )
    videos = sum(
        1
        for f in files
        if (f.content_type or "").split(";")[0].strip().lower().startswith("video/")
    )
    if videos > 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ALBUM_TOO_MANY_VIDEOS",
                "message": "an album may contain at most one video",
            },
        )


def _album_too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "code": "ALBUM_TOO_LARGE",
            "message": (
                f"album exceeds the {settings.max_album_mb} MiB total cap "
                "(MAX_ALBUM_MB)"
            ),
        },
    )


@router.post("/v1/captures/album", response_model=AlbumCaptureResponse)
async def album_capture(
    response: Response,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AlbumCaptureResponse:
    """Capture a carousel/album share as ONE memory.

    Accepts 2-30 files (multipart field `files`): image/* photos
    (png/jpeg/webp) and at most one video/*. Each file is validated with
    the same rules as /v1/captures/upload (content-type allowlist,
    per-file MAX_UPLOAD_MB, sanitized names, untrusted storage outside any
    web-served root, streaming sha256 writes); the whole album is capped
    at MAX_ALBUM_MB total.

    Dedup is by the combined content hash (order-independent): an
    already-READY album returns 200 with duplicate=true and stores no new
    bytes. Quota: one share = ONE capture against the free-tier bucket.

    The worker processes every photo (vision + OCR; the video, if present,
    goes through the normal video path) and tags each segment with its
    album_index, so ask/actions/brief citations can say "photo 7".
    Returns 202 (or 200 with duplicate=true).
    """
    # Quota first: an over-quota user must not be able to burn upload bytes.
    # One album = one capture, regardless of photo count.
    check_and_increment_quota(db, user, BUCKET_CAPTURES)
    validate_album_files(files)

    upload_dir = Path(settings.resolve_upload_dir())
    upload_dir.mkdir(parents=True, exist_ok=True)

    per_file_cap = settings.max_upload_mb * 1024 * 1024
    album_cap = settings.max_album_mb * 1024 * 1024
    # (part path, content_type, safe name, sha256, size)
    stored: list[tuple[Path, str, str, str, int]] = []
    current_part: Path | None = None
    cumulative = 0
    videos = 0
    try:
        for f in files:
            content_type = validate_upload_content_type(f.content_type)
            if content_type.startswith("video/"):
                videos += 1
                if videos > 1:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "ALBUM_TOO_MANY_VIDEOS",
                            "message": "an album may contain at most one video",
                        },
                    )
            remaining = album_cap - cumulative
            if remaining <= 0:
                raise _album_too_large()
            current_part = upload_dir / f".part-{uuid.uuid4().hex}"
            digest = write_upload_stream(
                f.file, current_part, min(per_file_cap, remaining)
            )
            size = current_part.stat().st_size
            cumulative += size
            stored.append(
                (current_part, content_type, sanitize_filename(f.filename),
                 digest, size)
            )
            current_part = None
    except Exception:
        for part, *_ in stored:
            part.unlink(missing_ok=True)
        if current_part is not None:
            current_part.unlink(missing_ok=True)
        raise

    hashes = [digest for _, _, _, digest, _ in stored]
    combined = album_combined_hash(hashes)

    def _discard_parts() -> None:
        for part, *_ in stored:
            part.unlink(missing_ok=True)

    # Duplicate pre-check BEFORE publishing: an already-READY album costs no
    # new bytes and no new AI (unlike the single-upload path, which stores
    # first — an album can be hundreds of MB, so check early).
    existing = (
        db.query(SourceItem)
        .filter(
            SourceItem.user_id == user.id,
            SourceItem.platform == "upload",
            SourceItem.platform_item_id == combined,
        )
        .first()
    )
    if existing is not None:
        memory = (
            db.query(Memory)
            .filter(Memory.source_item_id == existing.id)
            .first()
        )
        if (
            memory is not None
            and memory.processing_status == ProcessingStatus.READY.value
            and not should_reprocess(
                memory.processing_status,
                memory.processing_version,
                settings.processing_version,
            )
        ):
            _discard_parts()
            latest_capture = (
                db.query(Capture)
                .filter(Capture.source_item_id == existing.id)
                .order_by(Capture.captured_at.desc())
                .first()
            )
            response.status_code = 200
            return AlbumCaptureResponse(
                id=latest_capture.id,
                memory_id=memory.id,
                status=memory.processing_status,
                duplicate=True,
                file_count=len(files),
                content_hashes=hashes,
            )

    # Publish: atomic renames into the album layout; a concurrent identical
    # upload simply wins the race — same bytes, same names, harmless.
    names = [safe for _, _, safe, _, _ in stored]
    for idx, (part, content_type, _safe, digest, _size) in enumerate(stored):
        final = upload_dir / album_file_name(
            combined, idx, digest, _UPLOAD_EXTENSIONS.get(content_type, ".mp4")
        )
        try:
            part.replace(final)
        except OSError:
            if final.exists():
                part.unlink(missing_ok=True)  # lost the race; identical bytes
            else:
                _discard_parts()
                raise

    item = _get_or_create_item(
        db,
        user,
        platform="upload",
        platform_item_id=combined,
        canonical_url=f"upload://album/{combined}",
        original_url=", ".join(names)[:500] or f"album of {len(files)} files",
        source_status="UPLOADED",
    )

    resp, duplicate = _record_capture(db, user, item, capture_source="album")
    response.status_code = 200 if duplicate else 202
    return AlbumCaptureResponse(
        id=resp.id,
        memory_id=resp.memory_id,
        status=resp.status,
        duplicate=duplicate,
        file_count=len(files),
        content_hashes=hashes,
    )
