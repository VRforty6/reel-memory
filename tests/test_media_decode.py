"""Milestone 3 media decoding tests.

Unit tests stub the single subprocess boundary (media._run_cmd) — pure
logic, no binaries needed. The end-to-end test at the bottom uses a real
ffmpeg/ffprobe when present and is skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline import media
from app.pipeline.failures import FailureCode
from app.pipeline.state_machine import ProcessingStatus
from app.pipeline.worker import StageError, Worker

FFMPEG_PRESENT = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not FFMPEG_PRESENT, reason="ffmpeg/ffprobe not installed")


# --- fakes ------------------------------------------------------------------


def _probe_json(duration=42.5, width=1920, height=1080, audio=True, video=True):
    streams = []
    if video:
        streams.append({"codec_type": "video", "width": width, "height": height})
    if audio:
        streams.append({"codec_type": "audio"})
    payload = {"streams": streams}
    if duration is not None:
        payload["format"] = {"duration": str(duration)}
    return payload


class _FakeRunner:
    """Stands in for media._run_cmd. Records calls; ffprobe answers with the
    configured JSON, ffmpeg materializes output files (or fails per config)."""

    def __init__(
        self,
        probe_json=None,
        probe_rc=0,
        probe_raw=None,
        ffmpeg_rc=0,
        ffmpeg_stderr=b"ffmpeg boom",
        make_outputs=True,
        exc=None,
    ):
        self.calls: list[tuple[list[str], float]] = []
        self.probe_json = probe_json if probe_json is not None else _probe_json()
        self.probe_rc = probe_rc
        self.probe_raw = probe_raw
        self.ffmpeg_rc = ffmpeg_rc
        self.ffmpeg_stderr = ffmpeg_stderr
        self.make_outputs = make_outputs
        self.exc = exc

    def __call__(self, cmd, timeout_s):
        if self.exc is not None:
            raise self.exc
        self.calls.append((list(cmd), timeout_s))
        if "ffprobe" in cmd[0]:
            body = self.probe_raw if self.probe_raw is not None else json.dumps(
                self.probe_json
            ).encode()
            return subprocess.CompletedProcess(cmd, self.probe_rc, body, b"")
        if self.ffmpeg_rc != 0:
            return subprocess.CompletedProcess(cmd, self.ffmpeg_rc, b"", self.ffmpeg_stderr)
        if self.make_outputs:
            self._materialize(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def _materialize(self, cmd):
        out = cmd[-1]
        n = None
        if "-frames:v" in cmd:
            n = int(cmd[cmd.index("-frames:v") + 1])
        if "%03d" in out and n:
            for i in range(1, n + 1):
                Path(out.replace("%03d", f"{i:03d}")).write_bytes(b"fake-jpeg")
        else:
            Path(out).write_bytes(b"fake-wav-bytes")

    def ffmpeg_calls(self):
        return [c for c, _ in self.calls if "ffmpeg" in c[0]]


@pytest.fixture()
def runner(monkeypatch):
    r = _FakeRunner()
    monkeypatch.setattr(media, "_run_cmd", r)
    return r


@pytest.fixture()
def no_binaries(monkeypatch):
    monkeypatch.setattr(media, "binaries_available", lambda *a, **k: False)


# --- probing -----------------------------------------------------------------


def test_probe_parses_streams(runner):
    probe = media.probe_media("/tmp/x.mp4")
    assert probe.duration_s == pytest.approx(42.5)
    assert (probe.width, probe.height) == (1920, 1080)
    assert probe.has_audio and probe.has_video


def test_probe_rejects_ffprobe_failure(monkeypatch):
    r = _FakeRunner(probe_rc=1)
    monkeypatch.setattr(media, "_run_cmd", r)
    with pytest.raises(media.MediaDecodeError, match="could not read"):
        media.probe_media("/tmp/x.mp4")


def test_probe_rejects_garbage_json(monkeypatch):
    r = _FakeRunner(probe_raw=b"this is not json")
    monkeypatch.setattr(media, "_run_cmd", r)
    with pytest.raises(media.MediaDecodeError, match="unparseable"):
        media.probe_media("/tmp/x.mp4")


def test_probe_rejects_no_streams(monkeypatch):
    r = _FakeRunner(probe_json={"format": {}, "streams": []})
    monkeypatch.setattr(media, "_run_cmd", r)
    with pytest.raises(media.MediaDecodeError, match="no streams"):
        media.probe_media("/tmp/x.mp4")


def test_probe_tolerates_missing_duration(runner):
    runner.probe_json = _probe_json(duration=None)
    probe = media.probe_media("/tmp/x.mp4")
    assert probe.duration_s is None
    assert probe.has_video


# --- policy ------------------------------------------------------------------


def test_duration_over_cap_rejected():
    probe = media.MediaProbe(
        duration_s=601.0, width=1920, height=1080, has_audio=True, has_video=True
    )
    with pytest.raises(media.MediaDecodeError, match="exceeds.*600"):
        media.check_media_policy(probe, max_duration_s=600.0)


def test_duration_at_cap_allowed():
    probe = media.MediaProbe(
        duration_s=600.0, width=1920, height=1080, has_audio=True, has_video=True
    )
    media.check_media_policy(probe, max_duration_s=600.0)  # no raise


def test_unknown_duration_allowed():
    probe = media.MediaProbe(
        duration_s=None, width=1920, height=1080, has_audio=True, has_video=True
    )
    media.check_media_policy(probe, max_duration_s=600.0)  # no raise


# --- binaries ----------------------------------------------------------------


def test_missing_binaries_raise_with_install_hint(no_binaries, tmp_path):
    with pytest.raises(media.FfmpegNotAvailable) as ei:
        media.extract_audio("/tmp/x.mp4", tmp_path)
    assert "apt-get install ffmpeg" in str(ei.value)
    with pytest.raises(media.FfmpegNotAvailable):
        media.extract_frames("/tmp/x.mp4", tmp_path)


def test_binaries_available_checks_both(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    assert media.binaries_available() is True
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "ffprobe" else "/usr/bin/ffmpeg")
    assert media.binaries_available() is False


# --- audio -------------------------------------------------------------------


def test_extract_audio_no_audio_stream_returns_none(runner, tmp_path):
    runner.probe_json = _probe_json(audio=False)
    assert media.extract_audio("/tmp/silent.mp4", tmp_path) is None
    # ffmpeg was never invoked — only ffprobe ran.
    assert runner.ffmpeg_calls() == []


def test_extract_audio_builds_16k_mono_wav(runner, tmp_path):
    out = media.extract_audio("/tmp/clip.mp4", tmp_path)
    assert out is not None and out.endswith(".wav")
    assert Path(out).exists()
    argv = runner.ffmpeg_calls()[0]
    for flag in ("-nostdin", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"):
        assert flag in argv, argv
    # Safety cap against lying containers.
    assert "-t" in argv


def test_extract_audio_rejects_non_video(runner, tmp_path):
    runner.probe_json = _probe_json(video=False, audio=True)
    with pytest.raises(media.MediaDecodeError, match="no video stream"):
        media.extract_audio("/tmp/weird.mp4", tmp_path)


def test_extract_audio_ffmpeg_failure_raises(runner, tmp_path):
    runner.ffmpeg_rc = 1
    with pytest.raises(media.MediaDecodeError, match="could not extract audio"):
        media.extract_audio("/tmp/clip.mp4", tmp_path)
    # Partial output cleaned up.
    assert list(tmp_path.glob("*_audio.wav")) == []


def test_extract_audio_timeout_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media, "_run_cmd",
        _FakeRunner(exc=media.MediaDecodeError("ffmpeg timed out after 180s")),
    )
    with pytest.raises(media.MediaDecodeError, match="timed out"):
        media.extract_audio("/tmp/clip.mp4", tmp_path)


def test_extract_audio_enforces_duration_cap(runner, tmp_path):
    runner.probe_json = _probe_json(duration=601.0)
    with pytest.raises(media.MediaDecodeError, match="exceeds"):
        media.extract_audio("/tmp/long.mp4", tmp_path, max_duration_s=600.0)
    assert runner.ffmpeg_calls() == []  # never even tried


# --- frames ------------------------------------------------------------------


def test_extract_frames_spreads_evenly(runner, tmp_path):
    runner.probe_json = _probe_json(duration=60.0)
    frames = media.extract_frames("/tmp/clip.mp4", tmp_path, count=6)
    assert len(frames) == 6
    assert frames == sorted(frames)  # time order
    argv = runner.ffmpeg_calls()[0]
    vf = argv[argv.index("-vf") + 1]
    assert "fps=0.100000" in vf  # 6 frames / 60 s


def test_extract_frames_caps_resolution(runner, tmp_path):
    media.extract_frames("/tmp/clip.mp4", tmp_path, count=4, max_dimension_px=640)
    argv = runner.ffmpeg_calls()[0]
    vf = argv[argv.index("-vf") + 1]
    assert "min(iw,640)" in vf


def test_extract_frames_unknown_duration_still_capped(runner, tmp_path):
    runner.probe_json = _probe_json(duration=None)
    frames = media.extract_frames("/tmp/clip.mp4", tmp_path, count=5)
    assert len(frames) == 5  # -frames:v caps the total regardless


def test_extract_frames_no_video_raises(runner, tmp_path):
    runner.probe_json = _probe_json(video=False)
    with pytest.raises(media.MediaDecodeError, match="no video stream"):
        media.extract_frames("/tmp/audio.mp3", tmp_path)


def test_extract_frames_never_returns_empty_silently(runner, tmp_path):
    runner.make_outputs = False  # ffmpeg "succeeds" but yields nothing
    with pytest.raises(media.MediaDecodeError, match="could not sample frames"):
        media.extract_frames("/tmp/clip.mp4", tmp_path)


def test_extract_frames_enforces_duration_cap(runner, tmp_path):
    runner.probe_json = _probe_json(duration=700.0)
    with pytest.raises(media.MediaDecodeError, match="exceeds"):
        media.extract_frames("/tmp/long.mp4", tmp_path, max_duration_s=600.0)
    assert runner.ffmpeg_calls() == []


# --- worker wiring ------------------------------------------------------------


def _worker():
    return Worker(session_factory=lambda: None)


def test_worker_audio_maps_decode_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise media.MediaDecodeError("ffmpeg exploded")

    monkeypatch.setattr(media, "extract_audio", boom)
    with pytest.raises(StageError) as ei:
        _worker()._extract_audio("/tmp/x.mp4", tmp_path)
    assert ei.value.code == FailureCode.MEDIA_DECODE_FAILED


def test_worker_audio_none_passes_through(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "extract_audio", lambda *a, **k: None)
    assert _worker()._extract_audio("/tmp/silent.mp4", tmp_path) is None


def test_worker_frames_maps_decode_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise media.MediaDecodeError("ffprobe exploded")

    monkeypatch.setattr(media, "extract_frames", boom)
    with pytest.raises(StageError) as ei:
        _worker()._extract_frames("/tmp/x.mp4", tmp_path)
    assert ei.value.code == FailureCode.MEDIA_DECODE_FAILED


def test_worker_frames_returns_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media, "extract_frames", lambda *a, **k: ["f1.jpg", "f2.jpg"]
    )
    assert _worker()._extract_frames("/tmp/x.mp4", tmp_path) == ["f1.jpg", "f2.jpg"]


# --- album video with no audio: transcription skipped, not crashed -----------


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def delete(self):
        return 0

    def all(self):
        return []


class _FakeDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def query(self, *a, **k):
        return _FakeQuery()


def test_album_silent_video_skips_transcription(monkeypatch, tmp_path):
    """A video with no audio stream must not crash the album path: the
    transcription stage is skipped and vision/OCR still run."""
    from app.capture.canonicalize import CanonicalURL
    from app.config import settings
    from app.pipeline.providers import (
        OCRSegment,
        UnconfiguredMemoryGenerator,
        VisualObservation,
    )
    from app.sources.base import AlbumMedia, SourceMetadata

    monkeypatch.setattr(media, "extract_audio", lambda *a, **k: None)
    monkeypatch.setattr(media, "extract_frames", lambda *a, **k: ["/tmp/f.jpg"])

    class _Vision:
        def __init__(self):
            self.calls = 0

        def analyze_frames(self, frame_paths):
            self.calls += 1
            return [VisualObservation(timestamp_ms=0, description="seen")]

    class _OCR:
        def extract_text(self, frame_paths):
            return [OCRSegment(timestamp_ms=0, text="words")]

    class _Emb:
        def embed(self, texts):
            return [[0.0] * settings.embedding_dim for _ in texts]

    providers = SimpleNamespace(
        speech=SimpleNamespace(), vision=_Vision(), ocr=_OCR(),
        embedding=_Emb(), memory_generator=UnconfiguredMemoryGenerator(),
        chat=SimpleNamespace(), search=SimpleNamespace(),
    )
    db = _FakeDB()
    memory = SimpleNamespace(
        id=uuid.uuid4(), processing_status=ProcessingStatus.RESOLVING_SOURCE.value,
        title=None, summary=None, category=None, language=None,
        processing_version=None,
    )
    job = SimpleNamespace(
        status="QUEUED", stage=None, attempt_count=0,
        failure_code=None, failure_message=None,
        started_at=None, finished_at=None,
    )
    item = SimpleNamespace(
        platform="upload", caption=None, creator_handle=None, published_at=None,
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake-video")
    album = AlbumMedia(
        canonical=CanonicalURL(
            platform="upload", platform_item_id="d" * 64,
            canonical_url="upload://album/" + "d" * 64, original_url="album",
        ),
        metadata=SourceMetadata(),
        files=[str(video)],
    )

    Worker(session_factory=lambda: db, providers=providers)._process_album(
        db, memory, item, job, album, tmp_path
    )

    modalities = {getattr(o, "modality", None) for o in db.added}
    assert "speech" not in modalities  # no audio -> no transcription attempted
    assert "visual" in modalities and "ocr" in modalities
    assert memory.processing_status == ProcessingStatus.READY.value


# --- real ffmpeg integration -------------------------------------------------


def _make_test_video(path: Path, *, with_audio: bool, duration: int = 2) -> None:
    inputs = [
        "-f", "lavfi", "-i",
        f"testsrc=duration={duration}:size=320x240:rate=10",
    ]
    if with_audio:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    cmd = (
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
        + inputs
        + ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + (["-c:a", "aac"] if with_audio else [])
        + ["-shortest", "-y", str(path)]
    )
    cp = subprocess.run(cmd, capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode()[-500:]
    assert path.stat().st_size > 0


def _ffprobe_json(path: Path) -> dict:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, timeout=30,
    )
    assert cp.returncode == 0
    return json.loads(cp.stdout.decode())


@needs_ffmpeg
def test_end_to_end_real_ffmpeg(tmp_path):
    src = tmp_path / "reel.mp4"
    _make_test_video(src, with_audio=True)

    probe = media.probe_media(src)
    assert probe.has_video and probe.has_audio
    assert probe.duration_s == pytest.approx(2.0, abs=0.3)
    assert (probe.width, probe.height) == (320, 240)

    wav = media.extract_audio(src, tmp_path)
    assert wav is not None and Path(wav).stat().st_size > 44
    wav_info = _ffprobe_json(Path(wav))
    audio = [s for s in wav_info["streams"] if s["codec_type"] == "audio"][0]
    assert audio["sample_rate"] == "16000"
    assert audio["channels"] == 1

    frames = media.extract_frames(src, tmp_path, count=4, max_dimension_px=1280)
    assert len(frames) == 4
    for f in frames:
        assert Path(f).stat().st_size > 0
    frame_info = _ffprobe_json(Path(frames[0]))
    assert frame_info["streams"][0]["width"] == 320  # already under the cap


@needs_ffmpeg
def test_end_to_end_silent_video_returns_none(tmp_path):
    src = tmp_path / "silent.mp4"
    _make_test_video(src, with_audio=False)
    assert media.probe_media(src).has_audio is False
    assert media.extract_audio(src, tmp_path) is None
    frames = media.extract_frames(src, tmp_path, count=3)
    assert len(frames) == 3


@needs_ffmpeg
def test_end_to_end_corrupt_file_fails_honestly(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is definitely not a video file" * 100)
    with pytest.raises(media.MediaDecodeError):
        media.probe_media(bad)


# --- scene detection (FFmpeg 9 compatibility) --------------------------------


def test_scene_detection_uses_ffmpeg9_compatible_fps_mode(monkeypatch):
    """detect_scene_cuts must not use -vsync: FFmpeg 9 removed the deprecated
    alias and rejects the command. -fps_mode vfr is the supported
    equivalent (valid since FFmpeg 5.1)."""
    seen: list[list[str]] = []

    def fake_run(cmd, timeout_s):
        seen.append(list(cmd))
        if "ffprobe" in cmd[0]:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps(_probe_json()).encode(), b""
            )
        return subprocess.CompletedProcess(
            cmd, 0, b"",
            b"[Parsed_showinfo_2 @ 0x1] n:   0 pts_time:2.5 duration:0.1\n",
        )

    monkeypatch.setattr(media, "_run_cmd", fake_run)
    cuts = media.detect_scene_cuts("/tmp/clip.mp4")
    assert cuts == [2500]
    argv = seen[-1]
    assert "-fps_mode" in argv
    assert argv[argv.index("-fps_mode") + 1] == "vfr"
    assert "-vsync" not in argv


def _make_cut_video(path: Path, cut_at: float = 4.37, duration: float = 12.0) -> None:
    """Two solid colors joined by a single hard cut. The cut is placed
    between uniform-sample grid points (12 uniform frames over 12 s), so
    only scene detection can catch it."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=red:s=320x180:d={cut_at}:r=10",
        "-f", "lavfi", "-i",
        f"color=c=blue:s=320x180:d={duration - cut_at}:r=10",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-y", str(path),
    ]
    cp = subprocess.run(cmd, capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode()[-500:]
    assert path.stat().st_size > 0


@needs_ffmpeg
def test_scene_detection_finds_hard_cut_between_uniform_grid(tmp_path):
    """Regression: scene extraction must actually produce frames for a hard
    cut that uniform sampling misses. Proves the detection command runs on
    the installed FFmpeg (the -vsync form was rejected by FFmpeg 9)."""
    src = tmp_path / "cut.mp4"
    _make_cut_video(src, cut_at=4.37)

    cuts = media.detect_scene_cuts(src, scene_threshold=0.10)
    assert cuts, "scene detection found no cuts on a hard-cut video"
    assert any(abs(c - 4370) <= 300 for c in cuts), f"no cut near 4.37s: {cuts}"

    keys = media.extract_scene_keyframes(src, tmp_path, max_extra=12)
    assert keys, "no scene keyframes extracted"
    assert any(abs(ts - 4370) <= 300 for _, ts in keys), [ts for _, ts in keys]
    for p, _ in keys:
        assert Path(p).stat().st_size > 0
    # Bounded: 12 uniform + up to 12 scene frames per the visual-search contract.
    assert len(keys) <= 12
