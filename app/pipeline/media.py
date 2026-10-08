"""Local media decoding — Milestone 3 (PRD §17 pipeline).

ffmpeg/ffprobe-based audio + frame extraction for direct video uploads and
album videos. Media is treated as **hostile input**:

* probe with ffprobe *before* decoding — never trust the container at face value;
* enforce a max-duration cap (over-long videos fail honestly, they are not
  silently truncated);
* scale frames down to a max width so vision costs stay sane;
* hard timeouts on every subprocess; no shell invocation; ``-nostdin`` so
  ffmpeg can never block on stdin;
* all outputs land in the worker's per-job temp dir, which the worker deletes
  on every path (success or failure); partial outputs are also removed on
  failure inside this module.

Contract: on success the functions return real paths. On ANY failure they
raise :class:`MediaDecodeError` with a human-readable reason; the worker maps
that to the honest ``MEDIA_DECODE_FAILED`` taxonomy code. Empty results are
never returned silently — the only "no data" case is a video with no audio
stream, which returns ``None`` (the worker then skips transcription).

When ffmpeg/ffprobe are not installed, :class:`FfmpegNotAvailable` is raised
with install guidance instead of faking success.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger("reel-memory.media")

PathLike = Union[str, Path]

_FFMPEG_INSTALL_HINT = (
    "ffmpeg and ffprobe are required for video decoding but were not found. "
    "Install them and make sure both are on PATH (or set FFMPEG_BIN / "
    "FFPROBE_BIN): Debian/Ubuntu `sudo apt-get install ffmpeg`, "
    "macOS `brew install ffmpeg`, or add `ffmpeg` to your container image "
    "(see README 'Media decoding')."
)


class MediaDecodeError(Exception):
    """Anything that went wrong while probing or decoding media."""


class FfmpegNotAvailable(MediaDecodeError):
    """ffmpeg/ffprobe binaries are missing — honest failure, never faked."""


@dataclass(frozen=True)
class MediaProbe:
    """What ffprobe says about a file. All values come from the container and
    are treated as advisory, not trusted: durations are re-capped at decode
    time and outputs are size-checked."""

    duration_s: Optional[float]  # None when the container doesn't say
    width: Optional[int]
    height: Optional[int]
    has_audio: bool
    has_video: bool


# -- subprocess -------------------------------------------------------------


def _run_cmd(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    """Run one ffmpeg/ffprobe command. No shell, stdin closed, hard timeout.

    This is the single subprocess boundary in the module, so tests can
    monkeypatch it without touching the real binaries.
    """
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaDecodeError(
            f"{cmd[0]} timed out after {timeout_s:.0f}s on {cmd[-1]!r}; "
            "the file may be malformed or hostile"
        ) from e
    except OSError as e:
        # Binary vanished between the which() check and exec (or PATH changed).
        raise FfmpegNotAvailable(f"{_FFMPEG_INSTALL_HINT} ({e})") from e


def binaries_available(
    ffmpeg_bin: str = "ffmpeg", ffprobe_bin: str = "ffprobe"
) -> bool:
    """True when both binaries resolve on PATH. Pure (unit-testable)."""
    return shutil.which(ffmpeg_bin) is not None and shutil.which(ffprobe_bin) is not None


def _require_binaries(ffmpeg_bin: str, ffprobe_bin: str) -> None:
    if not binaries_available(ffmpeg_bin, ffprobe_bin):
        raise FfmpegNotAvailable(_FFMPEG_INSTALL_HINT)


def _stderr_tail(cp: subprocess.CompletedProcess, limit: int = 400) -> str:
    tail = cp.stderr.decode("utf-8", "replace").strip().splitlines()
    return "\n".join(tail[-8:])[-limit:]


# -- probing ----------------------------------------------------------------


def probe_media(
    path: PathLike, *, ffprobe_bin: str = "ffprobe", timeout_s: float = 15.0
) -> MediaProbe:
    """Probe a media file with ffprobe. Raises MediaDecodeError when the file
    is not a decodable container (garbage, truncated, unsupported)."""
    src = str(path)
    cp = _run_cmd(
        [
            ffprobe_bin,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            src,
        ],
        timeout_s,
    )
    if cp.returncode != 0:
        raise MediaDecodeError(
            f"ffprobe could not read {src!r} (not a media file, truncated, or "
            f"unsupported container): {_stderr_tail(cp)}"
        )
    try:
        data = json.loads(cp.stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as e:
        raise MediaDecodeError(f"ffprobe returned unparseable JSON for {src!r}: {e}") from e

    streams = data.get("streams") or []
    if not streams:
        raise MediaDecodeError(f"ffprobe found no streams in {src!r}")

    duration_s: Optional[float] = None
    raw_duration = (data.get("format") or {}).get("duration")
    if raw_duration is not None:
        try:
            duration_s = float(raw_duration)
        except (TypeError, ValueError):
            duration_s = None

    width = height = None
    has_audio = has_video = False
    for stream in streams:
        ctype = stream.get("codec_type")
        if ctype == "audio":
            has_audio = True
        elif ctype == "video":
            has_video = True
            if width is None:
                try:
                    width = int(stream.get("width") or 0) or None
                    height = int(stream.get("height") or 0) or None
                except (TypeError, ValueError):
                    width = height = None

    return MediaProbe(
        duration_s=duration_s,
        width=width,
        height=height,
        has_audio=has_audio,
        has_video=has_video,
    )


def check_media_policy(probe: MediaProbe, *, max_duration_s: float) -> None:
    """Enforce the duration cap. Over-long media fails honestly — it is not
    silently truncated, because the transcript would then misrepresent the
    video. Pure (unit-testable)."""
    if probe.duration_s is not None and probe.duration_s > max_duration_s:
        raise MediaDecodeError(
            f"video duration {probe.duration_s:.1f}s exceeds the "
            f"MAX_MEDIA_DURATION_S cap of {max_duration_s:.0f}s; "
            "split the video or raise the cap to process it"
        )


# -- audio ------------------------------------------------------------------


def extract_audio(
    path: PathLike,
    dest_dir: PathLike,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    max_duration_s: float = 600.0,
    probe_timeout_s: float = 15.0,
    decode_timeout_s: float = 180.0,
) -> Optional[str]:
    """Decode a video's audio track to 16 kHz mono WAV (what the speech
    provider expects). Returns the WAV path, or None when the container has
    no audio stream at all (the worker then skips transcription — honest,
    not an empty transcript). Raises MediaDecodeError on any failure."""
    _require_binaries(ffmpeg_bin, ffprobe_bin)
    src = str(path)
    probe = probe_media(src, ffprobe_bin=ffprobe_bin, timeout_s=probe_timeout_s)
    if not probe.has_video:
        raise MediaDecodeError(f"{src!r} has no video stream; refusing to decode")
    if not probe.has_audio:
        log.info("no audio stream in %r; skipping audio extraction", src)
        return None
    check_media_policy(probe, max_duration_s=max_duration_s)

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{uuid.uuid4().hex[:12]}_audio.wav"
    cmd = [
        ffmpeg_bin,
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vn",                      # audio only
        "-ac", "1",                 # mono
        "-ar", "16000",             # 16 kHz
        "-c:a", "pcm_s16le",        # uncompressed WAV
        "-t", f"{max_duration_s:.0f}",  # belt-and-suspenders: never trust container duration
        "-y", str(out),
    ]
    cp = _run_cmd(cmd, decode_timeout_s)
    if cp.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise MediaDecodeError(
            f"ffmpeg could not extract audio from {src!r}: {_stderr_tail(cp)}"
        )
    log.info("extracted audio %s (%d bytes)", out.name, out.stat().st_size)
    return str(out)


# -- frames -----------------------------------------------------------------


def extract_frames(
    path: PathLike,
    dest_dir: PathLike,
    *,
    count: int = 12,
    max_dimension_px: int = 1280,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    max_duration_s: float = 600.0,
    probe_timeout_s: float = 15.0,
    decode_timeout_s: float = 180.0,
) -> list[str]:
    """Sample up to `count` JPEG frames evenly spread across the video,
    scaled down so the width never exceeds `max_dimension_px` (keeps vision
    costs sane). Returns the frame paths in time order. Raises
    MediaDecodeError on any failure — never an empty list."""
    _require_binaries(ffmpeg_bin, ffprobe_bin)
    count = max(1, int(count))
    src = str(path)
    probe = probe_media(src, ffprobe_bin=ffprobe_bin, timeout_s=probe_timeout_s)
    if not probe.has_video:
        raise MediaDecodeError(f"{src!r} has no video stream; cannot sample frames")
    check_media_policy(probe, max_duration_s=max_duration_s)

    if probe.duration_s and probe.duration_s > 0:
        fps = count / probe.duration_s
    else:
        # Container won't say how long it is: sample at a modest fixed rate;
        # -frames:v still caps the total at `count`.
        fps = 0.2
    vf = f"fps={fps:.6f},scale=w='min(iw,{int(max_dimension_px)})':h=-2"

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:12]
    pattern = str(dest / f"{prefix}_frame_%03d.jpg")
    cmd = [
        ffmpeg_bin,
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vf", vf,
        "-frames:v", str(count),
        "-q:v", "3",
        "-t", f"{max_duration_s:.0f}",  # belt-and-suspenders: never trust container duration
        "-y", pattern,
    ]
    created: list[Path] = []
    try:
        cp = _run_cmd(cmd, decode_timeout_s)
        created = sorted(dest.glob(f"{prefix}_frame_*.jpg"))
        if cp.returncode != 0 or not created:
            raise MediaDecodeError(
                f"ffmpeg could not sample frames from {src!r}: {_stderr_tail(cp)}"
            )
    except Exception:
        for p in created:
            p.unlink(missing_ok=True)
        # Also sweep any frame files if the glob above never ran.
        if not created:
            for p in dest.glob(f"{prefix}_frame_*.jpg"):
                p.unlink(missing_ok=True)
        raise
    log.info("sampled %d frames from %r", len(created), src)
    return [str(p) for p in created]


def make_thumbnail_jpeg(
    frame_path: PathLike,
    dest_dir: PathLike,
    *,
    max_width_px: int = 320,
    ffmpeg_bin: str = "ffmpeg",
    timeout_s: float = 30.0,
) -> str:
    """Downscale one extracted frame to a small thumbnail JPEG.

    Used once per memory at ingest so the app can show thumbnail-forward
    cards without retaining any video frames. Raises MediaDecodeError on
    failure — callers treat thumbnails as best-effort and must never fail
    the whole job over one.
    """
    if shutil.which(ffmpeg_bin) is None:
        raise MediaDecodeError(f"ffmpeg binary {ffmpeg_bin!r} not found on PATH")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"thumb_{uuid.uuid4().hex[:12]}.jpg"
    cmd = [
        ffmpeg_bin,
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(frame_path),
        "-vf", f"scale=w='min(iw,{int(max_width_px)})':h=-2",
        "-frames:v", "1",
        "-q:v", "4",
        "-y", str(out),
    ]
    try:
        cp = _run_cmd(cmd, timeout_s)
        if cp.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise MediaDecodeError(
                f"ffmpeg could not make a thumbnail from {str(frame_path)!r}: "
                f"{_stderr_tail(cp)}"
            )
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return str(out)


# -- scene-aware keyframes (visual search supplementation) ------------------


def detect_scene_cuts(
    path: PathLike,
    *,
    scene_threshold: float = 0.10,
    max_cuts: int = 120,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    probe_timeout_s: float = 15.0,
    decode_timeout_s: float = 180.0,
) -> list[int]:
    """Detect scene-cut timestamps (milliseconds) with ffmpeg's ``select``
    filter on a downscaled stream. Returns sorted unique timestamps; an
    empty list means no cuts were found (not an error). Raises
    MediaDecodeError when the file is not decodable at all.

    The detection pass decodes at 320p into the null muxer, so it is cheap
    relative to full-res frame extraction. Timestamps come from showinfo's
    ``pts_time`` — true media timestamps, not estimates.
    """
    _require_binaries(ffmpeg_bin, ffprobe_bin)
    src = str(path)
    # Validate the container first: garbage in -> honest error, not [].
    probe_media(src, ffprobe_bin=ffprobe_bin, timeout_s=probe_timeout_s)

    cmd = [
        ffmpeg_bin,
        "-nostdin", "-hide_banner",
        "-i", src,
        "-vf",
        (
            "scale=w='min(iw,320)':h=-2,"
            f"select='gt(scene,{float(scene_threshold)})',showinfo"
        ),
        "-fps_mode", "vfr",  # FFmpeg 9 removed the deprecated -vsync alias
        "-frames:v", str(max(1, int(max_cuts))),
        "-f", "null", "-",
    ]
    # showinfo writes to stderr at info level; _run_cmd captures it.
    cp = _run_cmd(cmd, decode_timeout_s)
    if cp.returncode != 0:
        raise MediaDecodeError(
            f"ffmpeg scene detection failed for {src!r}: {_stderr_tail(cp)}"
        )
    import re

    cuts: list[int] = []
    for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", cp.stderr.decode("utf-8", "replace")):
        cuts.append(int(float(m.group(1)) * 1000))
    # The select filter never emits frame 0 (no previous frame to compare);
    # callers combine with uniform samples, which cover the start.
    return sorted(set(cuts))


def extract_scene_keyframes(
    path: PathLike,
    dest_dir: PathLike,
    *,
    max_extra: int = 12,
    scene_threshold: float = 0.10,
    max_dimension_px: int = 1280,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    probe_timeout_s: float = 15.0,
    decode_timeout_s: float = 180.0,
) -> list[tuple[str, int]]:
    """Bounded scene-aware keyframe supplementation.

    Detects up to ``max_extra`` scene cuts (evenly spaced when there are
    more cuts than the cap) and extracts one JPEG per cut at full
    ``max_dimension_px`` resolution via seek. Returns
    ``[(jpeg_path, timestamp_ms)]`` in time order. Returns [] when no cuts
    are found. Raises MediaDecodeError on fundamental decode problems.
    """
    max_extra = max(0, int(max_extra))
    if max_extra == 0:
        return []
    cuts = detect_scene_cuts(
        path,
        scene_threshold=scene_threshold,
        max_cuts=120,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        probe_timeout_s=probe_timeout_s,
        decode_timeout_s=decode_timeout_s,
    )
    if not cuts:
        return []
    # Evenly spaced selection so the extras represent the whole video rather
    # than clustering at the first cuts.
    if len(cuts) > max_extra:
        step = len(cuts) / max_extra
        cuts = [cuts[int(i * step)] for i in range(max_extra)]

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:12]
    out: list[tuple[str, int]] = []
    try:
        for i, ts_ms in enumerate(cuts):
            target = dest / f"{prefix}_scene_{i:03d}.jpg"
            cmd = [
                ffmpeg_bin,
                "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{ts_ms / 1000.0:.3f}",
                "-i", str(path),
                "-frames:v", "1",
                "-q:v", "3",
                "-vf", f"scale=w='min(iw,{int(max_dimension_px)})':h=-2",
                "-y", str(target),
            ]
            cp = _run_cmd(cmd, decode_timeout_s)
            if cp.returncode != 0 or not target.exists() or target.stat().st_size == 0:
                target.unlink(missing_ok=True)
                raise MediaDecodeError(
                    f"ffmpeg could not extract scene keyframe at {ts_ms}ms "
                    f"from {str(path)!r}: {_stderr_tail(cp)}"
                )
            out.append((str(target), ts_ms))
    except Exception:
        for p, _ in out:
            Path(p).unlink(missing_ok=True)
        raise
    log.info("extracted %d scene keyframes from %r", len(out), str(path))
    return out
