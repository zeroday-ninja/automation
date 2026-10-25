"""What a program is packaged from, and whether it follows the disk.

A program's runner is packaged once, when the program is created, so a preset
fix reaches only programs packaged afterwards. Two mechanisms exist to close
that: `POST /refresh-code`, which says it "repackages the program's runner
from the preset code *on disk*", and `improve_preset_code_digest`, which
answers "is this program's code current" so the dashboard can say otherwise.

Both read `_load_improve_preset_files`, which filled a module-level cache once
per process and never invalidated it. So on any service that had packaged a
program already, refresh-code repackaged the cache and reported success, and
the digest compared a stale digest against a stale digest and always answered
"current" — the stale-code notice could never fire for the case it exists for.
"""

from __future__ import annotations

import pytest

from openhands.automation import preset_router


@pytest.fixture
def preset_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway copy of the preset files, so an edit is safe to make."""
    sources = preset_router._improve_preset_sources()
    copied = {}
    for name, path in sources.items():
        destination = tmp_path / name
        destination.write_text(path.read_text())
        copied[name] = destination
    monkeypatch.setattr(preset_router, "_improve_preset_sources", lambda: dict(copied))
    # The cache is process-global, so a test that leaves it filled from the
    # real files would hide exactly the bug this file is about.
    monkeypatch.setattr(preset_router, "_IMPROVE_PRESET_CACHE", None)
    monkeypatch.setattr(preset_router, "_IMPROVE_PRESET_STAMP", None)
    return copied


def test_an_edit_on_disk_reaches_the_next_packaging(preset_dir) -> None:
    # Arrange: packaged once, which is what used to fill the cache for good.
    before = preset_router._load_improve_preset_files()["context.py"]
    assert "a brand new helper" not in before

    # Act: the preset changes on disk, as a merged fix does.
    preset_dir["context.py"].write_text(
        f"{before}\n\ndef a_brand_new_helper():\n    pass\n"
    )

    # Assert: the next read has it. Reporting success while packaging the old
    # code is worse than refusing, because the program then looks fixed.
    after = preset_router._load_improve_preset_files()["context.py"]
    assert "a_brand_new_helper" in after


def test_the_digest_notices_too(preset_dir) -> None:
    # Arrange
    before = preset_router.improve_preset_code_digest()

    # Act: a prompt edited, which is a change to what an agent is told and so
    # a change a program can be stale against.
    prompt = preset_dir["builder_prompt.md"]
    prompt.write_text(prompt.read_text() + "\n- And one more rule.\n")

    # Assert: a different digest, so `preset_code_is_current` can be false.
    # With the digest reading a frozen cache it could only ever be true.
    assert preset_router.improve_preset_code_digest() != before


def test_an_unchanged_disk_is_read_once(preset_dir) -> None:
    # The cache still has to be a cache: this runs on every program read, and
    # re-reading eleven files to decide whether to re-read eleven files would
    # be no cache at all.
    first = preset_router._load_improve_preset_files()
    second = preset_router._load_improve_preset_files()

    assert first is second
