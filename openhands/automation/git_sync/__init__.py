"""Bidirectional git sync for automations: any number of repositories per org.

Submodules:
    client.py           Async wrapper around the `git` CLI
    serializer.py       Automation <-> git file-tree (de)serializer, encryption
    loop.py             Sync cycle, background loop, mark_git_sync_dirty hook
    config_override.py  Per-remote runtime config (the /v1/git-sync/remotes API)
    secret_store.py     At-rest encryption for the stored token and key
    router.py           FastAPI remotes/assignments API plus the legacy
                        single-repo status/config/check/sync endpoints
    schemas.py          Request/response schemas for router.py
"""

from openhands.automation.git_sync.loop import (
    SyncCycleResult,
    git_sync_loop,
    is_git_sync_active,
    mark_git_sync_dirty,
    reassign_automation,
    run_sync_cycle,
)

# Named `git_sync_router`: re-exporting it as `router` would shadow the
# `router` submodule as a package attribute.
from openhands.automation.git_sync.router import router as git_sync_router


__all__ = [
    "SyncCycleResult",
    "git_sync_loop",
    "git_sync_router",
    "is_git_sync_active",
    "mark_git_sync_dirty",
    "reassign_automation",
    "run_sync_cycle",
]
