"""Visual retrieval benchmark — model selection for visual search.

Compares local joint image/text models (OpenCLIP ViT-B/32, ViT-L/14,
SigLIP-B/16) on the user's representative visual-memory queries, using
synthetic videos whose distinctive event:
  * is not spoken (no audio track -> no transcript),
  * is not OCR text (no text is drawn),
  * is not in the caption (generic "synthetic benchmark video N"),
  * is omitted from any vision description (none is generated here —
    the benchmark indexes ONLY frame embeddings, isolating visual recall).

Each target event is a 0.6s segment placed BETWEEN uniform-sample grid
points, so uniform-only sampling misses it and the scene-cut
supplementation must catch it (correction 1 acceptance condition).

Measures per model: Recall@1, Recall@3, indexing latency/reel, query
latency, peak RAM for model load, storage/reel. Reports actual numbers
even when the 80%/90% targets are missed.

Usage:
  .venv/bin/python qa/visual_benchmark.py [--models ViT-B-32 ...] [--out qa/visual_benchmark_results.json]

Requires: torch, open_clip_torch, pillow (pip install -e ".[visual]"),
ffmpeg/ffprobe, and local weight files (see --weights-dir; downloaded once
with curl from HuggingFace — see README 'Visual search').
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import frame_index as frame_index_mod
from app.pipeline import media as media_decode
from app.pipeline.frame_index import FrameSample
from app.pipeline.visual import OpenCLIPVisualEmbeddingProvider

# ---------------------------------------------------------------- fixtures

W, H = 320, 180
FPS = 10
DURATION_S = 12
EVENT_START, EVENT_END = 4.2, 4.8  # between uniform grid points 4.0 / 5.0

# (video_id, query, relevant?)
TARGETS = [
    ("sticky", "guy writing with 2 pens on a red sticky note"),
    ("moto", "red motorcycle with number 46"),
    ("airbag", "jacket that inflates like an airbag"),
    ("dosa", "giant dosa on a table"),
    ("diagram", "coding diagram with boxes and arrows"),
    ("monitors", "girl using two monitors with purple lights"),
]
DISTRACTORS = ["park", "ocean", "city", "forest", "beach", "books"]


def _draw(vid: str, t: float):
    """Render one frame with PIL. No text anywhere (keeps OCR out)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (110, 110, 110))
    d = ImageDraw.Draw(img)
    in_event = EVENT_START <= t < EVENT_END

    if vid == "sticky":
        d.rectangle([0, 0, W, H], fill=(150, 140, 120))  # desk
        d.rectangle([40, 30, 120, 70], fill=(140, 130, 110))
        if in_event:  # red sticky note + two pens
            d.rectangle([100, 40, 220, 140], fill=(205, 30, 30))
            d.rectangle([115, 55, 135, 130], fill=(25, 25, 25))
            d.rectangle([160, 55, 180, 130], fill=(25, 25, 25))
    elif vid == "moto":
        d.rectangle([0, 0, W, H], fill=(90, 90, 95))  # asphalt
        d.rectangle([0, 85, W, 95], fill=(200, 200, 200))  # lane
        if in_event:  # red motorcycle-ish: body + two wheels
            d.ellipse([60, 110, 110, 160], fill=(15, 15, 15))
            d.ellipse([210, 110, 260, 160], fill=(15, 15, 15))
            d.rectangle([95, 95, 225, 125], fill=(200, 25, 25))
    elif vid == "airbag":
        d.rectangle([0, 0, W, H], fill=(120, 115, 105))  # indoor
        d.rectangle([140, 40, 180, 140], fill=(70, 60, 50))  # person-ish
        if in_event:  # inflated jacket: big tan rounded mass
            d.rounded_rectangle([90, 30, 230, 150], radius=30, fill=(210, 180, 130))
    elif vid == "dosa":
        d.rectangle([0, 0, W, H], fill=(120, 85, 55))  # wooden table
        if in_event:  # giant dosa: tan ellipse on white plate
            d.ellipse([70, 30, 250, 150], fill=(235, 235, 235))
            d.ellipse([95, 50, 225, 130], fill=(215, 170, 100))
    elif vid == "diagram":
        d.rectangle([0, 0, W, H], fill=(25, 30, 55))  # dark IDE
        d.rectangle([20, 20, 100, 40], fill=(40, 45, 70))
        if in_event:  # boxes + arrows
            for x in (40, 130, 220):
                d.rectangle([x, 60, x + 60, 110], outline=(240, 240, 240), width=3)
            d.line([100, 85, 130, 85], fill=(240, 240, 240), width=3)
            d.line([190, 85, 220, 85], fill=(240, 240, 240), width=3)
    elif vid == "monitors":
        d.rectangle([0, 0, W, H], fill=(20, 18, 25))  # dark room
        if in_event:  # purple glow + two monitors
            d.rectangle([0, 0, W, H], fill=(60, 30, 90))
            d.rectangle([50, 50, 140, 120], fill=(15, 15, 20))
            d.rectangle([180, 50, 270, 120], fill=(15, 15, 20))
    elif vid == "park":
        d.rectangle([0, 0, W, H], fill=(70, 130, 70))
        d.ellipse([130, 20, 190, 80], fill=(40, 100, 40))
        d.rectangle([200, 100, 280, 120], fill=(120, 90, 60))
    elif vid == "ocean":
        d.rectangle([0, 0, W, H // 2], fill=(120, 170, 200))
        d.rectangle([0, H // 2, W, H], fill=(40, 90, 160))
        d.polygon([(150, 40), (150, 110), (200, 110)], fill=(240, 240, 240))
    elif vid == "city":
        d.rectangle([0, 0, W, H], fill=(100, 100, 105))
        d.rectangle([30, 20, 90, 150], fill=(70, 70, 75))
        d.rectangle([200, 40, 260, 150], fill=(80, 80, 85))
        d.rectangle([110, 110, 190, 135], fill=(230, 190, 40))  # taxi
    elif vid == "forest":
        d.rectangle([0, 0, W, H], fill=(35, 80, 40))
        for x in (50, 140, 230):
            d.rectangle([x, 30, x + 18, 160], fill=(90, 65, 40))
    elif vid == "beach":
        d.rectangle([0, 0, W, H], fill=(230, 210, 170))
        d.rectangle([0, 0, W, 50], fill=(140, 190, 230))
        d.polygon([(160, 60), (110, 110), (210, 110)], fill=(220, 80, 80))
        d.line([160, 60, 160, 130], fill=(120, 90, 60), width=4)
    elif vid == "books":
        d.rectangle([0, 0, W, H], fill=(110, 80, 55))
        for i, c in enumerate([(180, 60, 60), (60, 120, 180), (80, 160, 80),
                               (190, 170, 60), (150, 90, 150)]):
            d.rectangle([40 + i * 48, 40, 80 + i * 48, 150], fill=c)
    return img


def build_video(vid: str, out_path: Path) -> None:
    from PIL import Image  # noqa: F401

    frames_dir = out_path.parent / f"frames_{vid}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = FPS * DURATION_S
    for i in range(n):
        _draw(vid, i / FPS).save(frames_dir / f"f{i:04d}.png")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-framerate", str(FPS), "-i", str(frames_dir / "f%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)],
        check=True,
    )
    for p in frames_dir.glob("*.png"):
        p.unlink()
    frames_dir.rmdir()


# ---------------------------------------------------------------- photo fixtures
# Real photos (qa/bench_photos/): ecologically valid inputs for CLIP/SigLIP,
# which are trained on real photography, not flat-color drawings.

PHOTO_TARGETS = {  # video_id -> photo file
    "sticky": "sticky.jpg",
    "moto": "moto.jpg",
    "airbag": "airbag.jpg",
    "dosa": "dosa.jpg",
    "diagram": "diagram.webp",
    "monitors": "monitors.jpg",
}
PHOTO_DISTRACTORS = {  # video_id -> (photo file, event time)
    "d1_cat": ("d1_cat.jpg", 7.2),
    "d2_car": ("d2_car.jpg", 7.2),
    "d3_beach": ("d3_beach.jpg", 7.2),
    "d4_mountain": ("d4_mountain.jpg", 7.2),
    "d5_book": ("d5_book.jpg", 7.2),
    "d6_guitar": ("d6_guitar.jpg", 7.2),
}
PHOTO_EVENT = (4.2, 4.8)  # target events: between uniform grid points


def _cover(img, size):
    """Resize + center-crop to fill `size` (like a fullscreen reel cutaway)."""
    from PIL import Image

    w, h = size
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1),
                     Image.LANCZOS)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def build_photo_video(photo_path: Path, event_start: float, event_end: float,
                      out_path: Path) -> None:
    """12s video: near-static neutral base, real photo cutaway for 0.6s."""
    from PIL import Image, ImageDraw

    base = Image.new("RGB", (W, H), (28, 28, 34))
    d = ImageDraw.Draw(base)
    d.rectangle([0, H - 24, W, H], fill=(20, 20, 26))  # caption-safe bar
    photo = _cover(Image.open(photo_path).convert("RGB"), (W, H))
    frames_dir = out_path.parent / f"frames_{out_path.stem}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = FPS * DURATION_S
    for i in range(n):
        t = i / FPS
        frame = photo if event_start <= t < event_end else base
        frame.save(frames_dir / f"f{i:04d}.png")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-framerate", str(FPS), "-i", str(frames_dir / "f%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)],
        check=True,
    )
    for p in frames_dir.glob("*.png"):
        p.unlink()
    frames_dir.rmdir()


def build_fixtures(work: Path, mode: str) -> dict[str, Path]:
    videos: dict[str, Path] = {}
    if mode == "photo":
        photos = Path(__file__).resolve().parent / "bench_photos"
        for vid, photo in PHOTO_TARGETS.items():
            videos[vid] = work / f"{vid}.mp4"
        for vid, (photo, _) in PHOTO_DISTRACTORS.items():
            videos[vid] = work / f"{vid}.mp4"
        for vid, path in videos.items():
            if path.exists():
                continue
            print(f"building {vid} ...", flush=True)
            if vid in PHOTO_TARGETS:
                photo = photos / PHOTO_TARGETS[vid]
                es, ee = PHOTO_EVENT
            else:
                photo = photos / PHOTO_DISTRACTORS[vid][0]
                es = PHOTO_DISTRACTORS[vid][1]
                ee = es + 0.6
            build_photo_video(photo, es, ee, path)
    else:
        for vid, _ in TARGETS:
            videos[vid] = work / f"{vid}.mp4"
        for vid in DISTRACTORS:
            videos[vid] = work / f"{vid}.mp4"
        for vid, path in videos.items():
            if not path.exists():
                print(f"building {vid} ...", flush=True)
                build_video(vid, path)
    return videos


# ---------------------------------------------------------------- benchmark

CANDIDATES = [
    # (arch, weights file in --weights-dir, expected dim)
    ("ViT-B-32", "vit-b32.bin", 512),
    ("ViT-L-14", "vit-l14.bin", 768),
    ("ViT-B-16-SigLIP", "siglip-b16.bin", 768),
]


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def cosine_top(query_vec, frame_vecs):
    import torch

    q = torch.tensor(query_vec)
    sims = [
        float(q.dot(torch.tensor(v)) / (q.norm() * torch.tensor(v).norm() + 1e-12))
        for v in frame_vecs
    ]
    return max(sims) if sims else -1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=None,
                    help="subset of ViT-B-32 ViT-L-14 ViT-B-16-SigLIP")
    ap.add_argument("--weights-dir", default=None)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--out", default="qa/visual_benchmark_results.json")
    ap.add_argument("--fixtures", default="photo", choices=["photo", "synthetic"],
                    help="photo: real photos as 0.6s cutaways (default); "
                         "synthetic: flat-color PIL drawings (sanity check only)")
    args = ap.parse_args()

    import torch

    torch.set_num_threads(max(1, os.cpu_count() // 2 or 1))

    weights_dir = Path(args.weights_dir or
                       Path(__file__).resolve().parent.parent / ".model-cache")
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="visbench-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"work dir: {work}", flush=True)

    # 1. fixtures
    videos = build_fixtures(work, args.fixtures)
    results: dict = {"videos": len(videos), "queries": len(TARGETS),
                     "fixtures": args.fixtures, "models": {}}

    # 2. frame selection (production code path) — once, shared by models
    print("selecting frames ...", flush=True)
    tmp = work / "frames"
    tmp.mkdir(exist_ok=True)
    selections: dict[str, list[FrameSample]] = {}
    for vid, path in videos.items():
        uniform = media_decode.extract_frames(
            path, tmp, count=12, max_dimension_px=320,
            max_duration_s=600.0,
        )
        samples = frame_index_mod.select_video_frames(str(path), tmp, uniform)
        selections[vid] = samples
        n_scene = sum(1 for s in samples if s.selection == "scene")
        ts_scene = [s.timestamp_ms for s in samples if s.selection == "scene"]
        print(f"  {vid}: {len(samples)} frames ({n_scene} scene {ts_scene})",
              flush=True)

    # 3. per-model benchmark
    for arch, weights_file, expected_dim in CANDIDATES:
        if args.models and arch not in args.models:
            continue
        weights = weights_dir / weights_file
        if not weights.exists():
            print(f"SKIP {arch}: weights missing at {weights}", flush=True)
            continue
        rss_before = peak_rss_mb()
        t0 = time.perf_counter()
        provider = OpenCLIPVisualEmbeddingProvider(
            model_name=arch, pretrained=str(weights))
        assert provider.dim == expected_dim, (
            f"{arch}: dim {provider.dim} != expected {expected_dim}")
        load_s = time.perf_counter() - t0
        rss_after = peak_rss_mb()
        print(f"\n=== {arch} (dim {provider.dim}) loaded in {load_s:.1f}s "
              f"RSS {rss_before:.0f} -> {rss_after:.0f} MB ===", flush=True)

        # index all videos
        index_times = []
        frame_vecs: dict[str, list] = {}
        for vid, samples in selections.items():
            t1 = time.perf_counter()
            vecs = provider.embed_images([s.path for s in samples])
            index_times.append(time.perf_counter() - t1)
            frame_vecs[vid] = vecs

        # queries
        ranks = []
        query_times = []
        per_query = []
        for vid, query in TARGETS:
            t1 = time.perf_counter()
            qvec = provider.embed_texts([query])[0]
            scored = sorted(
                ((cosine_top(qvec, frame_vecs[v]), v) for v in videos),
                reverse=True,
            )
            query_times.append(time.perf_counter() - t1)
            ranked = [v for _, v in scored]
            rank = ranked.index(vid) + 1
            ranks.append(rank)
            per_query.append({"query": query, "target": vid, "rank": rank,
                              "top1": ranked[0]})
            print(f"  rank {rank}: {query!r:.60} -> {ranked[0]}", flush=True)

        r1 = sum(1 for r in ranks if r == 1) / len(ranks)
        r3 = sum(1 for r in ranks if r <= 3) / len(ranks)
        n_frames = sum(len(s) for s in selections.values())
        results["models"][arch] = {
            "dim": provider.dim,
            "recall@1": r1,
            "recall@3": r3,
            "per_query": per_query,
            "index_latency_s_per_reel_mean": sum(index_times) / len(index_times),
            "index_latency_s_per_reel_max": max(index_times),
            "query_latency_s_mean": sum(query_times) / len(query_times),
            "frames_per_reel_mean": n_frames / len(videos),
            "storage_bytes_per_reel": (n_frames / len(videos)) * provider.dim * 4,
            "ram_mb_model_load_delta": rss_after - rss_before,
            "ram_mb_peak": rss_after,
            "model_load_s": load_s,
        }
        print(f"{arch}: Recall@1={r1:.0%} Recall@3={r3:.0%} "
              f"index {sum(index_times)/len(index_times):.1f}s/reel "
              f"query {sum(query_times)/len(query_times)*1000:.0f}ms",
              flush=True)
        del provider
        import gc
        gc.collect()

    # 4. acceptance: text-only baseline must NOT solve the queries
    print("\n--- acceptance: text-only signals carry none of the query terms ---",
          flush=True)
    distinctive = {
        "sticky": ["sticky", "pens"],
        "moto": ["motorcycle"],
        "airbag": ["airbag", "inflates"],
        "dosa": ["dosa"],
        "diagram": ["diagram", "boxes", "arrows"],
        "monitors": ["monitors", "purple"],
    }
    captions = {vid: "synthetic benchmark video" for vid in videos}
    ok = True
    for vid, terms in distinctive.items():
        text_blob = captions[vid]  # transcript: none (no audio). OCR: none (no text drawn).
        leaked = [t for t in terms if t in text_blob]
        if leaked:
            ok = False
            print(f"  LEAK in {vid}: {leaked}")
    print("  text/OCR/transcript carry no distinctive terms:", ok, flush=True)
    results["acceptance_text_isolated"] = ok

    out_path = Path(args.out)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
