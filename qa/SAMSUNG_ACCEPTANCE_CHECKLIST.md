# Reel Memory A.2 — Samsung device acceptance checklist

Run on physical Samsung hardware. Emulator-only results do not count as acceptance.

## Test record

- Tester/date:
- Samsung model and region/carrier:
- Android version / One UI version:
- App version/build identifier:
- Install path: clean install / upgrade from prior APK:
- Backend URL and network: Wi-Fi / mobile / VPN:
- Battery mode: Optimized / Restricted / Power saving:
- Evidence folder (screen recording, screenshots, logs):

For every case record **PASS / FAIL / BLOCKED**, elapsed time where requested, and evidence filename.

## 1. Install, launch, and account

- [ ] Clean install launches without crash and displays the expected signed-out state.
- [ ] Sign in, background the app, relaunch, and verify the session remains valid.
- [ ] Sign out; verify queued private data and authentication behavior match the product decision.
- [ ] Rotate, switch dark/light mode, change Samsung font scale to Large, and confirm controls remain reachable.

## 2. Samsung share-sheet capture

- [ ] From Instagram, share a reel URL to Reel Memory; return to Instagram promptly and see one acknowledgement.
- [ ] From Samsung Internet, share a normal article URL; verify one queued capture and eventual ready state.
- [ ] From Samsung Gallery, share one image and one video separately.
- [ ] From Samsung Gallery, share 2–30 images as one album; verify one album/memory, preserved order, and correct photo count.
- [ ] Share a mixed album containing images plus one video; verify it is accepted as one album.
- [ ] Share an album with two videos; verify a clear rejection and no partial queue/files.
- [ ] Share unsupported content through My Files; verify a clear rejection and no crash.
- [ ] Confirm the share target remains available after device reboot and app update.

## 3. Duplicate and concurrency checks

- [ ] Share the same Instagram URL twice sequentially; verify one local source item and no second processing job.
- [ ] Rapidly invoke the same URL share twice; verify the database still contains one source item.
- [ ] Share the exact same Gallery image/video twice; verify one local upload identity, one effective upload, and no duplicate retained file.
- [ ] Share the same album twice; verify one effective upload and one memory.
- [ ] Trigger two different shares rapidly; verify neither is dropped or attributed to the other.

## 4. Process death and durable queue recovery

- [ ] Start a URL sync, then run `adb shell am force-stop dev.reelmemory.app`; relaunch and verify `SYNCING` recovers.
- [ ] Start a video upload, force-stop during `UPLOADING`, relaunch, and verify it resumes/retries exactly once.
- [ ] Repeat process-death recovery for an album upload.
- [ ] Disable Wi-Fi/mobile data before sharing; verify the item stays queued. Restore network and verify automatic completion.
- [ ] Reboot with queued work; unlock the phone and verify work resumes without opening duplicate jobs.
- [ ] Swipe the app away from Recents during upload; verify durable recovery.

## 5. Samsung background restrictions

- [ ] Under Settings → Battery → Background usage limits, place the app in Sleeping apps; document expected versus actual recovery.
- [ ] Repeat with Deep sleeping apps; verify the user receives an honest blocked/pending state rather than a false success.
- [ ] Enable Power saving mode during a queued upload, then disable it; verify one eventual completion.
- [ ] Leave the phone locked for 15+ minutes during processing; unlock and verify state refreshes.
- [ ] Switch between Wi-Fi and mobile data mid-upload; verify no duplicate row/upload and a recoverable outcome.

## 6. Limits, failures, and storage

- [ ] Test 0-byte/unreadable content provider input; verify rejection and cleanup.
- [ ] Test just below, exactly at, and just above the 200 MB per-file limit.
- [ ] Test album total just below and above 400 MB.
- [ ] Revoke source URI access or remove the source during capture; verify no crash and no orphaned partial file.
- [ ] Fill device storage close to capacity, attempt capture, and verify a clear failure plus partial-file cleanup.
- [ ] Return HTTP 401, 402, 413, 422, and 5xx from the test backend; verify correct local state and retry/paywall behavior.

## 7. Room database upgrade and state integrity

- [ ] Install the last released APK, create queued/synced rows, then install A.2 over it without clearing data.
- [ ] Verify Room migrations 1→4 preserve all old rows and the app opens without destructive migration.
- [ ] Verify album parent/child rows and order survive restart and upgrade.
- [ ] Confirm no row remains indefinitely in `SYNCING` or `UPLOADING` after force-stop/reboot.
- [ ] Confirm attempts, progress, errors, remote memory ID, and timestamps remain consistent after retry.

## 8. Ready-state product flow

- [ ] Successful upload progresses once through pending → uploading → processing → ready.
- [ ] Open the resulting memory and verify thumbnail, type, title/summary, and source attribution.
- [ ] Ask a question about a video/image/album and verify cited evidence matches the media; album citations identify the correct photo.
- [ ] Trigger backend processing failure and verify a terminal, actionable error instead of endless Processing.

## Release gate

- [ ] All blocker findings in `qa/MILESTONE_A2_QA_BASELINE.md` are fixed and independently rerun.
- [ ] Automated backend tests and Android `test lintDebug` pass from a clean environment.
- [ ] No generated `.gradle*`, `build/`, `local.properties`, signing, or Android Studio files appear in the submitted change set.
- [ ] Every checklist item has a result and device evidence; no unresolved FAIL remains.

