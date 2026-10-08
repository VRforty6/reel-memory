"""Suite-wide provider hermeticity (ITEM 6).

The backend reads AI provider selection from `*_PROVIDER` env vars at
`app.config.settings` import time. Without this file, a developer checkout
that exports real-provider settings (e.g. OPENAI_API_KEY=sk-... plus
SPEECH_PROVIDER=openai / VISUAL_EMBEDDING_PROVIDER=openclip) leaks into the
test process: tests that assume the default stub providers then fail or —
worse — attempt real network/model loads (OpenCLIP weights download).

This conftest runs before any test module imports `app.config`, so it can
pin the whole suite to the stub providers. A test that explicitly wants a
real provider opts in by monkeypatching `settings` attributes (the suite
already does this in several places) — nothing here prevents that.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

# Force the stub/default provider selection for every test, regardless of
# the developer's exported environment. These mirror the config defaults.
os.environ["SPEECH_PROVIDER"] = "none"
os.environ["VISION_PROVIDER"] = "none"
os.environ["OCR_PROVIDER"] = "none"
os.environ["EMBEDDING_PROVIDER"] = "none"
os.environ["MEMORY_GENERATOR_PROVIDER"] = "deterministic"
os.environ["CHAT_PROVIDER"] = "none"
os.environ["SEARCH_PROVIDER"] = "none"
os.environ["VISUAL_EMBEDDING_PROVIDER"] = "none"

# Drop API keys so no code path can accidentally construct a real cloud
# provider from the ambient environment. Tests needing a key set it
# explicitly on `settings` (see test_providers.py / test_qa.py).
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("SEARCH_API_KEY", None)


@pytest.fixture
def tmp_path():
    """Workspace-local temp path that works in restricted Windows runners.

    Pytest's default numbered directory applies POSIX-style chmod(0700).
    Some Windows sandbox ACL bridges translate that into an unreadable
    directory. These tests need isolation, not pytest's numbering machinery,
    so use a unique workspace directory and clean it after each test.
    """
    root = Path.cwd() / ".pytest-work"
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
