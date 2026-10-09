"""Local/offline AI providers for Reel Memory.

These providers never send media or text to a remote AI API. Heavy models are
loaded lazily on first use so API startup stays fast and tests can import the
module without optional dependencies installed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.pipeline.providers import OCRProvider, OCRSegment, ProviderError, SpeechProvider, TranscriptSegment

log = logging.getLogger("reel-memory.providers.local")


class FasterWhisperSpeechProvider(SpeechProvider):
    """Local faster-whisper transcription with timestamped segments."""

    def __init__(self) -> None:
        self._model = None

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise ProviderError(
                "faster-whisper is not installed; install the local provider dependencies"
            ) from e
        from app.config import settings

        try:
            self._model = WhisperModel(
                settings.local_whisper_model,
                device=settings.local_whisper_device,
                compute_type=settings.local_whisper_compute_type,
                cpu_threads=settings.local_whisper_cpu_threads,
                download_root=settings.local_model_cache_dir,
            )
        except Exception as e:  # model/runtime boundary
            raise ProviderError(f"failed to load local Whisper model: {type(e).__name__}: {e}") from e
        return self._model

    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        path = Path(audio_path)
        if not path.is_file():
            raise ProviderError(f"audio file does not exist: {audio_path}")
        from app.config import settings

        try:
            segments, info = self._get_model().transcribe(
                str(path),
                beam_size=settings.local_whisper_beam_size,
                vad_filter=True,
            )
            language = getattr(info, "language", None)
            out: list[TranscriptSegment] = []
            for seg in segments:
                text = (seg.text or "").strip()
                if not text:
                    continue
                out.append(
                    TranscriptSegment(
                        start_ms=max(0, int(float(seg.start) * 1000)),
                        end_ms=max(0, int(float(seg.end) * 1000)),
                        text=text,
                        lang=language,
                    )
                )
            return out
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"local Whisper transcription failed: {type(e).__name__}: {e}") from e


class RapidOCRProvider(OCRProvider):
    """CPU-local OCR using RapidOCR/ONNX Runtime.

    Frame timestamps are approximate because the current worker provider
    contract passes paths rather than FrameSample objects. Exact text search is
    unaffected; a later contract change can preserve precise frame timestamps.
    """

    def __init__(self) -> None:
        self._engine = None

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise ProviderError(
                "rapidocr-onnxruntime is not installed; install the local provider dependencies"
            ) from e
        try:
            self._engine = RapidOCR()
        except Exception as e:
            raise ProviderError(f"failed to initialize local OCR: {type(e).__name__}: {e}") from e
        return self._engine

    def extract_text(self, frame_paths: list[str]) -> list[OCRSegment]:
        if not frame_paths:
            return []
        from app.config import settings
        try:
            import cv2
        except ImportError as e:
            raise ProviderError("opencv-python is required by the local OCR provider") from e

        # Evenly preserve coverage when the media decoder produced more frames
        # than the OCR budget. This avoids a first-N bias on longer Reels.
        limit = max(1, int(settings.local_ocr_max_frames))
        if len(frame_paths) <= limit:
            selected = list(enumerate(frame_paths))
        else:
            positions = [round(i * (len(frame_paths) - 1) / (limit - 1)) for i in range(limit)] if limit > 1 else [0]
            selected = [(i, frame_paths[i]) for i in positions]

        out: list[OCRSegment] = []
        for original_idx, frame_path in selected:
            image = cv2.imread(frame_path)
            if image is None:
                raise ProviderError(f"local OCR could not decode frame {original_idx}: {frame_path}")
            h, w = image.shape[:2]
            max_dim = max(320, int(settings.local_ocr_max_dimension_px))
            if max(h, w) > max_dim:
                scale = max_dim / float(max(h, w))
                image = cv2.resize(
                    image,
                    (max(1, round(w * scale)), max(1, round(h * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            try:
                result, _elapsed = self._get_engine()(image)
            except Exception as e:
                raise ProviderError(
                    f"local OCR failed on frame {original_idx}: {type(e).__name__}: {e}"
                ) from e
            if not result:
                continue
            lines: list[str] = []
            for row in result:
                if not row or len(row) < 2:
                    continue
                text = str(row[1]).strip()
                if text:
                    lines.append(text)
            if lines:
                out.append(OCRSegment(timestamp_ms=original_idx * 5000, text="\n".join(lines)))
        return out
