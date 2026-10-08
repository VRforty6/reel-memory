# Reel Memory A.2 independent QA baseline

Date: 2026-09-28

Status: **not release-ready**. This is an independent result, not an acceptance of another agent's completion report.

## Executed checks

- Backend syntax: `python -m compileall -q app tests` — PASS.
- Dependency-free backend core: canonicalization, dedupe, retry/failure, hybrid ranking, and state-machine tests — **36 passed**.
- Full backend `pytest` — BLOCKED during collection because the machine lacks project dependencies (`fastapi`, `PyJWT`, `pydantic-settings`). An isolated `uv` run could not fetch PyPI because outbound network is blocked.
- Android `test lintDebug` — BLOCKED by shared-machine build contention after reaching Kotlin/Room processing. The first run hit an unwritable user-level Kotlin daemon path and an unreadable generated release-resource directory. A debug-only, in-process, single-worker rerun then waited behind other Java/Gradle activity without task output and was stopped rather than killing another agent's processes. This is not recorded as an app test failure; a clean rerun is still required.
- Git-based change review — BLOCKED because this delivered workspace has no `.git` directory. Pam stored a 208-file SHA-256 source manifest in her private agent workspace for later file-level comparison (`EBD3F621EEC424725F1C4FB8D72CC11DC9D7BD03DDCB0A421404308C73BDB675`).

## Verified findings

### A2-QA-001 — interrupted work can remain stuck forever (release blocker)

`UploadWorker` changes video and album rows to `UPLOADING`, while `SyncWorker` changes URL rows to `SYNCING`. Their next-run queries only select `PENDING` and `FAILED`. If Android kills the process, the device restarts, or the worker is stopped after that state change, the durable row is never selected again. `StatusCheckWorker` only recovers `PROCESSING`, not `UPLOADING` or `SYNCING`.

Required acceptance: stale in-flight rows must be atomically claimed with a lease or reset/recovered on worker start. Add tests for process death after claim and before an HTTP response for video, album, Instagram URL, and web URL queues.

### A2-QA-002 — local media upload duplication is not prevented (release blocker)

Each single media share is copied to a random filename and inserted into `video_uploads` with no content hash, source identity, or unique index. Re-sharing the same file creates another local row and another network upload. Albums likewise use a random local album directory and have no local uniqueness key. Server content-hash dedupe may avoid duplicate artificial-intelligence work, but it does not prevent duplicate device storage, queue entries, or upload traffic.

Required acceptance: persist a deterministic content hash/idempotency key, enforce it in Room with a unique index, and test sequential plus concurrent duplicate shares.

### A2-QA-003 — URL dedupe is check-then-insert without a Room unique constraint

`queued_shares` searches by `(platform, platformItemId)` before insert, and `web_captures` counts by URL before insert, but neither entity defines the corresponding unique index. Concurrent share intents can both observe no row and insert duplicates. `OnConflictStrategy.IGNORE` on `web_captures` does nothing without a conflicting constraint.

Required acceptance: add Room unique indices and make insertion itself the atomic dedupe operation. Add a concurrent insert test.

### A2-QA-004 — generated Gradle cache is not excluded

`android/.gitignore` excludes `.gradle/`, but a generated `android/.gradle-user/` exists and is not matched. At inspection it contained 4,815 files and about 951 MB. Because this workspace lacks Git metadata, `git check-ignore` cannot be run, but the path mismatch is direct.

Required acceptance: ignore `.gradle-user/` (or all intentional local Gradle user-home paths), remove it from any submitted change set, and verify with `git check-ignore -v android/.gradle-user/<file>` in a real checkout.

### A2-QA-005 — backend duplicate creation has a concurrency risk

The backend has the correct unique constraint on `(user_id, platform, platform_item_id)`, but `_get_or_create_item` performs SELECT then INSERT/flush without handling a uniqueness race. Two simultaneous identical captures can make one request fail at flush instead of returning a duplicate response. This requires a real PostgreSQL concurrency test before acceptance.

## Missing automated coverage

- Room migrations 1→2, 2→3, 3→4, and 1→4 on a preserved database.
- Recovery of stale `SYNCING` and `UPLOADING` rows.
- Atomic local dedupe for concurrent share intents.
- WorkManager behavior across force-stop, reboot, offline/online transition, Samsung battery restriction, and app update.
- Instrumented share-sheet tests; current Android coverage is local Java Virtual Machine tests only.
