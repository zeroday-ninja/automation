"""Improvement programs: a rotation over repositories, reviewed by a second runtime.

An improvement program is an ordinary cron automation created through
``POST /v1/preset/improve``. Each run takes the repository *after* the one the
previous run took, researches an improvement against the repository's own
stated identity and goals, implements it, and then has a different agent
runtime review the result adversarially before anything is proposed to the
user.

Submodules:
    rotation.py  Resolving the repository list and claiming the next one
    schemas.py   Request/response schemas for router.py
    router.py    The /v1/improve API used by a run and by the dashboard
    config.py    The program configuration stored in ``preset_metadata``
"""

from openhands.automation.improve.config import ImproveProgramConfig
from openhands.automation.improve.rotation import (
    NoRepositoriesError,
    RotationRepository,
    claim_next_cycle,
    next_builder,
    next_index,
    resolve_rotation,
    strip_credentials,
)

# Named `improve_router` for the same reason git_sync does: re-exporting it as
# `router` would shadow the `router` submodule as a package attribute.
from openhands.automation.improve.router import router as improve_router


__all__ = [
    "ImproveProgramConfig",
    "NoRepositoriesError",
    "RotationRepository",
    "claim_next_cycle",
    "improve_router",
    "next_builder",
    "next_index",
    "resolve_rotation",
    "strip_credentials",
]
