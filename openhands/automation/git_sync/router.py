"""FastAPI router for the git sync API.

Every endpoint acts on the caller's organization (`user.org_id`): git sync is
org-scoped, so an admin sees and changes only their own org's remotes.

Two surfaces share one implementation:

- `/v1/git-sync/remotes...` and `/v1/git-sync/assignments...`: any number of
  repositories per org, each with its own config, status and trigger, and
  the automation-to-remote assignments.
- the single-repo `/status`, `/config`, `/check` and `/sync`: the org's
  *default* remote, kept for clients that predate remotes. `PUT /config`
  creates the default remote on first save, so a client that knows only
  these endpoints still configures and syncs one repository as before.
"""

import asyncio
import logging
import uuid
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.config import GitSyncSettings, ServiceSettings, get_config
from openhands.automation.db import get_session
from openhands.automation.git_sync.client import GitSyncError, check_remote_access
from openhands.automation.git_sync.config_override import (
    apply_git_sync_config_override,
    apply_remote_config_override,
    candidate_settings_for,
    create_remote,
    delete_remote,
    effective_interval_for,
    effective_settings_for,
    find_remote_using_repo,
    get_default_remote,
    get_or_create_default_remote,
    get_remote,
    list_remotes,
    lock_repo_identity,
    org_has_remotes,
    resolve_candidate_git_sync_settings,
    resolve_effective_git_sync_settings,
    set_default_remote,
    states_of,
)
from openhands.automation.git_sync.loop import (
    lease_is_live,
    reassign_automation,
    run_sync_cycle,
    slugs_taken_by,
)
from openhands.automation.git_sync.schemas import (
    GitSyncAssignment,
    GitSyncAssignmentsResponse,
    GitSyncAssignmentUpdateRequest,
    GitSyncCheckResponse,
    GitSyncConfigUpdateRequest,
    GitSyncRemoteCreateRequest,
    GitSyncRemoteResponse,
    GitSyncRemotesResponse,
    GitSyncRemoteUpdateRequest,
    GitSyncStatusResponse,
    GitSyncTriggerResponse,
)
from openhands.automation.git_sync.secret_store import GitSyncSecretStoreError
from openhands.automation.git_sync.serializer import compute_slug
from openhands.automation.models import (
    Automation,
    AutomationGitSyncRemote,
    AutomationGitSyncState,
)


logger = logging.getLogger("automation.git_sync")

router = APIRouter(prefix="/v1/git-sync", tags=["Git Sync"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")

# Strong references to in-flight manual-trigger tasks: asyncio holds only weak
# ones, so a task could otherwise be GC'd mid-run.
_background_sync_tasks: Final[set[asyncio.Task[None]]] = set()

# Tighter than the sync cycle's per-command timeout, because an operator waits
# on a form for it and an unroutable host blocks the full duration.
_CHECK_TIMEOUT_SECONDS: Final[float] = 20.0

# Maps the config-update request's short field names to GitSyncSettings attrs.
_CONFIG_OVERRIDE_FIELDS: Final[dict[str, str]] = {
    "enabled": "git_sync_enabled",
    "interval_seconds": "git_sync_interval_seconds",
    "repo_url": "git_sync_repo_url",
    "branch": "git_sync_branch",
    "path": "git_sync_path",
    "token": "git_sync_token",
    "encryption_key": "git_sync_encryption_key",
    "author_name": "git_sync_author_name",
    "author_email": "git_sync_author_email",
}

# The settings that say which repo a remote syncs to; changing one of them is
# what can collide with another remote.
_REPO_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"git_sync_repo_url", "git_sync_branch", "git_sync_path"}
)

_REPO_TAKEN_DETAIL: Final[str] = (
    "Another repository configuration already syncs this repository, branch "
    "and path. Sharing them would import each other's automations; use a "
    "different repository or path."
)


def _config_overrides(data: GitSyncConfigUpdateRequest) -> dict[str, Any]:
    """The settings overrides a request body asks for, keyed by settings field.

    Only `GitSyncConfigUpdateRequest`'s own fields: the remote requests add
    `label` and `is_default`, which are columns, not overrides.
    """
    update = data.model_dump(exclude_unset=True, include=set(_CONFIG_OVERRIDE_FIELDS))
    return {_CONFIG_OVERRIDE_FIELDS[key]: value for key, value in update.items()}


# --- Status ---------------------------------------------------------------------


async def _dirty_count(
    session: AsyncSession, remote: AutomationGitSyncRemote | None, org_id: uuid.UUID
) -> int:
    where = (
        states_of(remote)
        if remote is not None
        else (AutomationGitSyncState.org_id == org_id)
        & AutomationGitSyncState.remote_id.is_(None)
    )
    count = await session.scalar(
        select(func.count())
        .select_from(AutomationGitSyncState)
        .where(where, AutomationGitSyncState.dirty.is_(True))
    )
    return count or 0


async def _unassigned_count(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Live automations no remote owns: no state row, or one with no remote."""
    count = await session.scalar(
        select(func.count())
        .select_from(Automation)
        .outerjoin(
            AutomationGitSyncState,
            AutomationGitSyncState.automation_id == Automation.id,
        )
        .where(
            Automation.org_id == org_id,
            Automation.deleted_at.is_(None),
            AutomationGitSyncState.remote_id.is_(None),
        )
    )
    return count or 0


async def _automation_count(
    session: AsyncSession, remote: AutomationGitSyncRemote
) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(AutomationGitSyncState)
        .join(Automation, Automation.id == AutomationGitSyncState.automation_id)
        .where(
            AutomationGitSyncState.remote_id == remote.id,
            Automation.deleted_at.is_(None),
        )
    )
    total = count or 0
    if remote.is_default:
        total += await _unassigned_count(session, remote.org_id)
    return total


async def _status_fields(
    session: AsyncSession,
    remote: AutomationGitSyncRemote | None,
    git_settings: GitSyncSettings,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    sync_started_at = remote.sync_started_at if remote is not None else None
    sync_in_progress = lease_is_live(sync_started_at, git_settings)
    return {
        "enabled": git_settings.enabled,
        "repo_url": git_settings.git_sync_repo_url,
        "branch": git_settings.git_sync_branch,
        "path": git_settings.git_sync_path,
        "encryption_enabled": bool(git_settings.git_sync_encryption_key),
        "interval_seconds": (
            effective_interval_for(remote) if remote is not None else 0
        ),
        "last_synced_commit": (
            remote.last_synced_commit if remote is not None else None
        ),
        "last_synced_at": remote.last_run_at if remote is not None else None,
        "last_error": (remote.last_error or None) if remote is not None else None,
        "last_error_at": remote.last_error_at if remote is not None else None,
        "dirty_count": await _dirty_count(session, remote, org_id),
        "sync_in_progress": sync_in_progress,
        "sync_started_at": sync_started_at if sync_in_progress else None,
    }


async def _build_status_response(
    session: AsyncSession, org_id: uuid.UUID, git_settings: GitSyncSettings
) -> GitSyncStatusResponse:
    """The legacy status: the default remote's, or the defaults without one."""
    remote = await get_default_remote(session, org_id)
    return GitSyncStatusResponse(
        **await _status_fields(session, remote, git_settings, org_id)
    )


async def _build_remote_response(
    session: AsyncSession, remote: AutomationGitSyncRemote
) -> GitSyncRemoteResponse:
    git_settings = effective_settings_for(remote)
    return GitSyncRemoteResponse(
        id=remote.id,
        label=remote.label or "",
        is_default=remote.is_default,
        automation_count=await _automation_count(session, remote),
        **await _status_fields(session, remote, git_settings, remote.org_id),
    )


# --- Shared steps -------------------------------------------------------------


async def _refuse_taken_repo(
    session: AsyncSession,
    candidate: GitSyncSettings,
    *,
    exclude_remote_id: uuid.UUID | None,
) -> None:
    """409 when another remote already syncs `candidate`'s repo, branch and
    path. Each remote's export writes `{path}/{slug}/` and its import reads
    every directory there, so two sharing them would import each other's
    automations.

    The identity lock is held until this request's session commits, so a
    concurrent save of the same repo waits and then sees this one.
    """
    if not candidate.git_sync_repo_url:
        return
    await lock_repo_identity(session, candidate)
    if (
        await find_remote_using_repo(
            session, candidate, exclude_remote_id=exclude_remote_id
        )
        is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_REPO_TAKEN_DETAIL
        )


async def _check(candidate: GitSyncSettings) -> GitSyncCheckResponse:
    """`ok` is reachability only; see `check_remote_access`."""
    if not candidate.git_sync_repo_url:
        return GitSyncCheckResponse(ok=False, detail="No repository URL is configured.")
    try:
        branch_exists = await check_remote_access(
            candidate.git_sync_repo_url,
            candidate.git_sync_branch,
            candidate.git_sync_token,
            min(candidate.git_sync_git_timeout_seconds, _CHECK_TIMEOUT_SECONDS),
        )
    except GitSyncError as e:
        # A failed check is a successful answer about the configuration, not a
        # failed request -- 200 with `ok: false`.
        logger.info("Git sync configuration check failed: %s", e)
        return GitSyncCheckResponse(ok=False, detail=str(e))
    return GitSyncCheckResponse(ok=True, branch_exists=branch_exists)


async def _run_sync_cycle_background(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    remote_id: uuid.UUID,
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> None:
    try:
        result = await run_sync_cycle(
            session_factory, org_id, git_settings, service_settings, remote_id=remote_id
        )
    except Exception:
        logger.exception(
            "Manually triggered git sync cycle failed for remote %s", remote_id
        )
        return
    if result.skipped:
        logger.info(
            "Manually triggered git sync for remote %s skipped: a cycle was "
            "already running",
            remote_id,
        )


def _schedule_cycle(
    request: Request,
    remote: AutomationGitSyncRemote,
    git_settings: GitSyncSettings,
) -> GitSyncTriggerResponse:
    """Start a cycle for `remote` now, instead of at the next poll.

    Fire-and-forget: returns once the cycle is scheduled, not once it completes
    (the same pattern as sandbox cleanup in router.py). 503 while the remote
    is paused or has no repo; `triggered: false` when a cycle already holds
    the remote's lease -- it picks up everything this one would have.
    """
    if not git_settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Git sync is not enabled",
        )
    if lease_is_live(remote.sync_started_at, git_settings):
        return GitSyncTriggerResponse(triggered=False)

    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    task = asyncio.create_task(
        _run_sync_cycle_background(
            session_factory,
            remote.org_id,
            remote.id,
            git_settings,
            get_config().service,
        )
    )
    _background_sync_tasks.add(task)
    task.add_done_callback(_background_sync_tasks.discard)
    return GitSyncTriggerResponse(triggered=True)


async def _save_overrides(
    session: AsyncSession,
    remote: AutomationGitSyncRemote,
    overrides: dict[str, Any],
    user: AuthenticatedUser,
) -> None:
    try:
        await apply_remote_config_override(
            session, remote, overrides, configured_by_user_id=user.user_id
        )
    except GitSyncSecretStoreError as e:
        # Refusing the write is the point: storing the token unencrypted would
        # silently downgrade exactly what this protects.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot store the git sync secrets securely: {e}",
        ) from e


async def _remote_or_404(
    session: AsyncSession, org_id: uuid.UUID, remote_id: uuid.UUID
) -> AutomationGitSyncRemote:
    remote = await get_remote(session, org_id, remote_id)
    if remote is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Git sync remote not found"
        )
    return remote


# --- Remotes --------------------------------------------------------------------


@router.get("/remotes")
async def list_git_sync_remotes(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncRemotesResponse:
    """Every repository the caller's org syncs, default first."""
    remotes = [
        await _build_remote_response(session, remote)
        for remote in await list_remotes(session, user.org_id)
    ]
    return GitSyncRemotesResponse(
        remotes=remotes,
        unassigned_count=await _unassigned_count(session, user.org_id),
    )


@router.post("/remotes", status_code=status.HTTP_201_CREATED)
async def create_git_sync_remote(
    data: GitSyncRemoteCreateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncRemoteResponse:
    """Add a repository. The org's first remote becomes its default, as does
    one created with `is_default: true`; the rest only ever hold automations
    imported from them or moved to them."""
    overrides = _config_overrides(data)
    will_be_default = bool(data.is_default) or not await org_has_remotes(
        session, user.org_id
    )
    candidate = candidate_settings_for(None, overrides, as_default=will_be_default)
    await _refuse_taken_repo(session, candidate, exclude_remote_id=None)

    remote = await create_remote(
        session,
        user.org_id,
        label=data.label or "",
        configured_by_user_id=user.user_id,
        make_default=bool(data.is_default),
    )
    await _save_overrides(session, remote, overrides, user)
    await session.commit()
    await session.refresh(remote)
    return await _build_remote_response(session, remote)


@router.post("/remotes/check")
async def check_new_git_sync_remote(
    data: GitSyncConfigUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncCheckResponse:
    """Test a repository before adding it. Nothing is saved and no sync runs."""
    as_default = not await org_has_remotes(session, user.org_id)
    return await _check(
        candidate_settings_for(None, _config_overrides(data), as_default=as_default)
    )


@router.get("/remotes/{remote_id}")
async def get_git_sync_remote(
    remote_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncRemoteResponse:
    remote = await _remote_or_404(session, user.org_id, remote_id)
    return await _build_remote_response(session, remote)


@router.put("/remotes/{remote_id}")
async def update_git_sync_remote(
    remote_id: uuid.UUID,
    data: GitSyncRemoteUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncRemoteResponse:
    """Reconfigure, pause/resume, rename or promote a remote, without a restart.

    Everything takes effect immediately. `is_default: true` makes it the org's
    default; the previous default keeps its automations.
    """
    remote = await _remote_or_404(session, user.org_id, remote_id)
    overrides = _config_overrides(data)

    if data.is_default is False and remote.is_default:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "An organization gives up its default remote by making another "
                "one the default, not by unsetting it."
            ),
        )
    if data.is_default:
        await set_default_remote(session, remote)
    if _REPO_IDENTITY_FIELDS & overrides.keys():
        candidate = candidate_settings_for(remote, overrides)
        await _refuse_taken_repo(session, candidate, exclude_remote_id=remote.id)
    if data.label is not None:
        remote.label = data.label[:255]
    await _save_overrides(session, remote, overrides, user)
    await session.commit()
    await session.refresh(remote)
    return await _build_remote_response(session, remote)


@router.delete("/remotes/{remote_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_git_sync_remote(
    remote_id: uuid.UUID,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Stop syncing to a repository. Its automations stay and are adopted by
    the default remote on its next cycle; the repository is left untouched."""
    remote = await _remote_or_404(session, user.org_id, remote_id)
    await delete_remote(session, remote)
    await session.commit()


@router.post("/remotes/{remote_id}/check")
async def check_git_sync_remote(
    remote_id: uuid.UUID,
    data: GitSyncConfigUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncCheckResponse:
    """Test what `PUT` with this body would leave in place, without saving it.

    Not gated on sync being enabled: the point is to get the repo URL and
    token right before turning it on. Reaching a caller-chosen URL is no new
    capability, since `PUT` already sets it under the same permission.
    """
    remote = await _remote_or_404(session, user.org_id, remote_id)
    return await _check(candidate_settings_for(remote, _config_overrides(data)))


@router.post("/remotes/{remote_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_git_sync_remote(
    remote_id: uuid.UUID,
    request: Request,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncTriggerResponse:
    remote = await _remote_or_404(session, user.org_id, remote_id)
    return _schedule_cycle(request, remote, effective_settings_for(remote))


# --- Assignments ----------------------------------------------------------------


def _assignment(
    automation: Automation,
    state: AutomationGitSyncState | None,
    default: AutomationGitSyncRemote | None,
) -> GitSyncAssignment:
    remote_id = state.remote_id if state is not None else None
    return GitSyncAssignment(
        automation_id=automation.id,
        name=automation.name,
        remote_id=remote_id,
        effective_remote_id=(
            remote_id
            if remote_id is not None
            else (default.id if default is not None else None)
        ),
        slug=state.slug if state is not None else None,
        dirty=state.dirty if state is not None else True,
    )


@router.get("/assignments")
async def list_git_sync_assignments(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncAssignmentsResponse:
    """Which remote each of the org's live automations syncs to."""
    rows = (
        await session.execute(
            select(Automation, AutomationGitSyncState)
            .outerjoin(
                AutomationGitSyncState,
                AutomationGitSyncState.automation_id == Automation.id,
            )
            .where(Automation.org_id == user.org_id, Automation.deleted_at.is_(None))
            .order_by(Automation.name, Automation.id)
        )
    ).all()
    default = await get_default_remote(session, user.org_id)
    return GitSyncAssignmentsResponse(
        automations=[
            _assignment(automation, state, default) for automation, state in rows
        ]
    )


@router.put("/assignments/{automation_id}")
async def update_git_sync_assignment(
    automation_id: uuid.UUID,
    data: GitSyncAssignmentUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncAssignment:
    """Move an automation to a remote. The previous remote's repo loses its
    directory on that remote's next cycle; the new one gains it on its own."""
    automation = await session.get(Automation, automation_id)
    if (
        automation is None
        or automation.org_id != user.org_id
        or automation.deleted_at is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found"
        )
    remote = await _remote_or_404(session, user.org_id, data.remote_id)

    state = await session.get(AutomationGitSyncState, automation.id)
    if state is None:
        taken = await slugs_taken_by(session, remote.id, remote.org_id)
        state = AutomationGitSyncState(
            automation_id=automation.id,
            org_id=automation.org_id,
            remote_id=remote.id,
            slug=compute_slug(automation.name, automation.id, taken=taken),
            dirty=True,
        )
        session.add(state)
        await session.flush()
    else:
        await reassign_automation(session, state, remote)
    await session.commit()
    await session.refresh(state)
    return _assignment(
        automation, state, await get_default_remote(session, user.org_id)
    )


# --- Legacy single-repo surface: the default remote -----------------------------


@router.get("/status")
async def get_git_sync_status(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncStatusResponse:
    """Report the default remote's configuration and last-sync state."""
    git_settings = await resolve_effective_git_sync_settings(session, user.org_id)
    return await _build_status_response(session, user.org_id, git_settings)


@router.put("/config")
async def update_git_sync_config(
    data: GitSyncConfigUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncStatusResponse:
    """Configure, reconfigure or pause/resume the default remote.

    Created on first save, so a client that knows only this endpoint still
    configures one repository as before remotes existed. Configuring a repo
    is what turns sync on, so everything here takes effect immediately. The
    caller is recorded as the remote's configuring user: automations imported
    from git are created as them (see loop.py).

    Refused with a 409 when another remote already syncs the same repository,
    branch and path.
    """
    overrides = _config_overrides(data)

    if _REPO_IDENTITY_FIELDS & overrides.keys():
        existing = await get_default_remote(session, user.org_id)
        candidate = await resolve_candidate_git_sync_settings(
            session, user.org_id, overrides
        )
        await _refuse_taken_repo(
            session,
            candidate,
            exclude_remote_id=existing.id if existing is not None else None,
        )

    try:
        await apply_git_sync_config_override(
            session, user.org_id, overrides, configured_by_user_id=user.user_id
        )
    except GitSyncSecretStoreError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot store the git sync secrets securely: {e}",
        ) from e
    await session.commit()

    git_settings = await resolve_effective_git_sync_settings(session, user.org_id)
    return await _build_status_response(session, user.org_id, git_settings)


@router.post("/check")
async def check_git_sync_config(
    data: GitSyncConfigUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncCheckResponse:
    """Test what `PUT /config` with this body would leave in place for the
    default remote, without saving it. See `check_git_sync_remote`."""
    candidate = await resolve_candidate_git_sync_settings(
        session, user.org_id, _config_overrides(data)
    )
    return await _check(candidate)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_git_sync(
    request: Request,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncTriggerResponse:
    """Trigger a cycle of the default remote now, instead of at the next poll."""
    git_settings = await resolve_effective_git_sync_settings(session, user.org_id)
    if not git_settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Git sync is not enabled",
        )
    # Local mode may have configured the repo from env alone, with no row yet;
    # the cycle needs one to carry its lease and status.
    remote = await get_or_create_default_remote(session, user.org_id)
    await session.commit()
    return _schedule_cycle(request, remote, git_settings)
