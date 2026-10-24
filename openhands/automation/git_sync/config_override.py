"""Per-remote runtime config for git sync, on top of the env defaults.

An organization syncs to any number of *remotes* -- one row of
`automation_git_sync_remote` each, holding the repository, branch, path,
credentials and interval saved from the Git Sync page. The router and the sync
loop merge a remote's overrides blob over the env defaults to get the settings
a cycle runs with.

Exactly one remote per org may be the *default*: automations that were never
assigned to a remote are adopted by it (see `_backfill_missing_states` in
loop.py), and the legacy single-repo endpoints (`/v1/git-sync/status`,
`/config`, `/check`, `/sync`) act on it. In local mode the env-level repo
settings (`AUTOMATION_GIT_SYNC_REPO_URL` and friends) are the default remote's
base configuration, as they always were; every other remote starts blank.
"""

import hashlib
import json
import uuid
from typing import Any, Final

from sqlalchemy import ColumnElement, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import GitSyncSettings, get_config
from openhands.automation.db import using_sqlite
from openhands.automation.git_sync.secret_store import (
    decrypt_secret_fields,
    encrypt_secret_fields,
)
from openhands.automation.models import (
    AutomationGitSyncRemote,
    AutomationGitSyncState,
)


# Runtime-only config: no env var, set solely from the UI. It rides in the same
# blob but is not a GitSyncSettings field, so it is filtered out before merging.
SYNC_INTERVAL_OVERRIDE_KEY: Final[str] = "git_sync_interval_seconds"

# 0 means manual-only: nothing syncs until a sync is triggered by hand.
DEFAULT_SYNC_INTERVAL_SECONDS: Final[int] = 0

# The fields that say *which* repo a remote syncs to. Two remotes sharing all
# three -- in one org or across orgs -- would import each other's automations,
# so creating or saving one refuses that.
_REPO_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "git_sync_repo_url",
    "git_sync_branch",
    "git_sync_path",
)

# The fields whose change can fix what an auth-failure streak was about.
_AUTH_BACKOFF_RESET_FIELDS: Final[tuple[str, ...]] = (
    "git_sync_repo_url",
    "git_sync_branch",
    "git_sync_token",
)


def base_git_sync_settings() -> GitSyncSettings:
    """The env-level defaults the *default* remote's overrides merge over.

    In local mode the env vars configure that one remote outright, as they
    configured the one repo before remotes existed. Elsewhere the deployment
    is shared by every org, so the fields that name a repo are blanked: an
    `AUTOMATION_GIT_SYNC_REPO_URL` set on a cloud deployment must not sync
    every org into one repo. The neutral defaults (branch, path, author,
    timeout) still apply to all of them.
    """
    config = get_config()
    if config.service.is_local_mode:
        return config.git_sync
    return neutral_git_sync_settings()


def neutral_git_sync_settings() -> GitSyncSettings:
    """The env defaults with every repo-naming field blanked.

    What a remote other than the default merges its overrides over: the env
    repo, token and encryption key describe one repository, and a second
    remote must never inherit them.
    """
    return get_config().git_sync.model_copy(
        update={
            "git_sync_repo_url": "",
            "git_sync_token": "",
            "git_sync_encryption_key": "",
        }
    )


def base_settings_for(remote: AutomationGitSyncRemote | None) -> GitSyncSettings:
    """The env defaults `remote`'s overrides merge over.

    `None` stands for a remote that does not exist yet and would become the
    default (the legacy endpoints configuring an org's first repo).
    """
    if remote is None or remote.is_default:
        return base_git_sync_settings()
    return neutral_git_sync_settings()


# --- Rows ---------------------------------------------------------------------


def states_of(remote: AutomationGitSyncRemote) -> ColumnElement[bool]:
    """The state rows `remote`'s next cycle exports: its own, plus -- for the
    org's default -- the org's unassigned ones, which it adopts first."""
    if not remote.is_default:
        return AutomationGitSyncState.remote_id == remote.id
    return or_(
        AutomationGitSyncState.remote_id == remote.id,
        (AutomationGitSyncState.org_id == remote.org_id)
        & AutomationGitSyncState.remote_id.is_(None),
    )


async def get_remote(
    session: AsyncSession, org_id: uuid.UUID, remote_id: uuid.UUID
) -> AutomationGitSyncRemote | None:
    """The remote, if it belongs to `org_id`: a caller never sees another
    org's rows, even by guessing an id."""
    remote = await session.get(AutomationGitSyncRemote, remote_id)
    if remote is None or remote.org_id != org_id:
        return None
    return remote


async def list_remotes(
    session: AsyncSession, org_id: uuid.UUID
) -> list[AutomationGitSyncRemote]:
    """The org's remotes, default first, then oldest first."""
    result = await session.execute(
        select(AutomationGitSyncRemote)
        .where(AutomationGitSyncRemote.org_id == org_id)
        .order_by(
            AutomationGitSyncRemote.is_default.desc(),
            AutomationGitSyncRemote.created_at,
            AutomationGitSyncRemote.id,
        )
    )
    return list(result.scalars().all())


async def get_default_remote(
    session: AsyncSession, org_id: uuid.UUID
) -> AutomationGitSyncRemote | None:
    result = await session.execute(
        select(AutomationGitSyncRemote).where(
            AutomationGitSyncRemote.org_id == org_id,
            AutomationGitSyncRemote.is_default.is_(True),
        )
    )
    return result.scalars().first()


async def org_has_remotes(session: AsyncSession, org_id: uuid.UUID) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(AutomationGitSyncRemote)
        .where(AutomationGitSyncRemote.org_id == org_id)
    )
    return bool(count)


async def create_remote(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    label: str = "",
    configured_by_user_id: uuid.UUID | None = None,
    make_default: bool = False,
) -> AutomationGitSyncRemote:
    """Add a remote to the org; an org's first remote is always its default.

    Two writers can race to create an org's first remote (a `PUT /config` and
    the local loop's tick, say). The INSERT runs in a SAVEPOINT so the loser's
    collision on the one-default-per-org index rolls back only that statement
    and it retries as a non-default remote -- or, for the default-on-demand
    callers, reads the winner's row.
    """
    become_default = make_default or not await org_has_remotes(session, org_id)
    if make_default and become_default:
        current = await get_default_remote(session, org_id)
        if current is not None:
            current.is_default = False
            await session.flush()
    try:
        async with session.begin_nested():
            remote = AutomationGitSyncRemote(
                id=uuid.uuid4(),
                org_id=org_id,
                label=(label or "").strip()[:255],
                is_default=become_default,
                overrides="{}",
                configured_by_user_id=configured_by_user_id,
            )
            session.add(remote)
            await session.flush()
        return remote
    except IntegrityError:
        if not become_default:
            raise
        # Someone else just created the org's default; this one is extra.
        async with session.begin_nested():
            remote = AutomationGitSyncRemote(
                id=uuid.uuid4(),
                org_id=org_id,
                label=(label or "").strip()[:255],
                is_default=False,
                overrides="{}",
                configured_by_user_id=configured_by_user_id,
            )
            session.add(remote)
            await session.flush()
        return remote


async def get_or_create_default_remote(
    session: AsyncSession, org_id: uuid.UUID
) -> AutomationGitSyncRemote:
    """The org's default remote, created empty on first use.

    What the legacy single-repo endpoints act on, and what local mode's loop
    creates so an env-configured repo has a row to carry its lease and status.
    """
    remote = await get_default_remote(session, org_id)
    if remote is not None:
        return remote
    try:
        async with session.begin_nested():
            remote = AutomationGitSyncRemote(
                id=uuid.uuid4(), org_id=org_id, is_default=True, overrides="{}"
            )
            session.add(remote)
            await session.flush()
        return remote
    except IntegrityError:
        remote = await get_default_remote(session, org_id)
        if remote is None:  # pragma: no cover - the other writer rolled back
            raise
        return remote


async def set_default_remote(
    session: AsyncSession, remote: AutomationGitSyncRemote
) -> None:
    """Make `remote` the org's default. Nothing moves: automations already
    assigned to the previous default stay there; only automations assigned to
    no remote go to the new one, on its next cycle."""
    if remote.is_default:
        return
    current = await get_default_remote(session, remote.org_id)
    if current is not None:
        current.is_default = False
        # Flushed first, or the partial unique index sees two defaults.
        await session.flush()
    remote.is_default = True
    await session.flush()


async def delete_remote(session: AsyncSession, remote: AutomationGitSyncRemote) -> None:
    """Stop syncing to `remote`.

    Its automations stay and become unassigned (the foreign key clears their
    `remote_id`): the org's default remote adopts them on its next cycle. The
    repository itself is left exactly as it is -- deleting from a repo this
    service no longer syncs is not its call. The pending removals cascade.
    """
    await session.execute(
        update(AutomationGitSyncState)
        .where(AutomationGitSyncState.remote_id == remote.id)
        .values(remote_id=None, dirty=True)
    )
    await session.delete(remote)
    await session.flush()


# --- Settings -----------------------------------------------------------------


def _decode_overrides(remote: AutomationGitSyncRemote | None) -> dict[str, Any]:
    if remote is None or not remote.overrides:
        return {}
    return decrypt_secret_fields(json.loads(remote.overrides))


def _merge(git_settings: GitSyncSettings, overrides: dict[str, Any]) -> GitSyncSettings:
    settings_overrides = {
        key: value
        for key, value in overrides.items()
        if key != SYNC_INTERVAL_OVERRIDE_KEY
    }
    if not settings_overrides:
        return git_settings
    return git_settings.model_copy(update=settings_overrides)


def _interval_from(overrides: dict[str, Any]) -> int:
    # A stored value that isn't a non-negative int is ignored rather than left
    # to crash or busy-spin the sync loop.
    value = overrides.get(SYNC_INTERVAL_OVERRIDE_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_SYNC_INTERVAL_SECONDS
    return value


def effective_settings_for(remote: AutomationGitSyncRemote) -> GitSyncSettings:
    """The settings an already-loaded remote resolves to."""
    return _merge(base_settings_for(remote), _decode_overrides(remote))


def effective_interval_for(remote: AutomationGitSyncRemote) -> int:
    """Seconds between the remote's automatic syncs; 0 means manual-only."""
    return _interval_from(_decode_overrides(remote))


async def resolve_effective_git_sync_settings(
    session: AsyncSession, org_id: uuid.UUID
) -> GitSyncSettings:
    """The org's default remote's settings, or the defaults when it has none.

    What the legacy single-repo endpoints report.
    """
    remote = await get_default_remote(session, org_id)
    return _merge(base_settings_for(remote), _decode_overrides(remote))


def candidate_settings_for(
    remote: AutomationGitSyncRemote | None,
    updates: dict[str, Any],
    *,
    as_default: bool | None = None,
) -> GitSyncSettings:
    """The settings a save of `updates` onto `remote` would leave in place.

    The same merge as apply + resolve, but persisting nothing: the check
    endpoints test a configuration before it is saved. `None` is a remote
    about to be created; `as_default` says whether it would be the org's
    default (and so inherit the env-level repo), defaulting to yes for the
    legacy single-repo endpoints. Handles the None-clears-the-override case,
    where the effective value is the env default, not a blank.
    """
    if remote is None and as_default is not None:
        base = base_git_sync_settings() if as_default else neutral_git_sync_settings()
    else:
        base = base_settings_for(remote)
    overrides = _decode_overrides(remote)
    for key, value in updates.items():
        # Dropped here as it is on the way out of storage: not a settings
        # field, and `model_copy` would graft it onto the model unvalidated.
        if key == SYNC_INTERVAL_OVERRIDE_KEY:
            continue
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    return _merge(base, overrides)


async def resolve_candidate_git_sync_settings(
    session: AsyncSession, org_id: uuid.UUID, updates: dict[str, Any]
) -> GitSyncSettings:
    """`candidate_settings_for` the org's default remote (legacy endpoints)."""
    return candidate_settings_for(await get_default_remote(session, org_id), updates)


async def resolve_effective_sync_interval_seconds(
    session: AsyncSession, org_id: uuid.UUID
) -> int:
    """The default remote's interval; 0 means manual-only (or no remote)."""
    remote = await get_default_remote(session, org_id)
    return effective_interval_for(remote) if remote is not None else 0


async def apply_remote_config_override(
    session: AsyncSession,
    remote: AutomationGitSyncRemote,
    updates: dict[str, Any],
    *,
    configured_by_user_id: uuid.UUID | None,
) -> None:
    """Persist partial overrides for `remote`, keyed by `GitSyncSettings` field.

    A `None` clears that field's override (reverting to the env default)
    rather than being stored literally -- the settings fields are plain
    str/bool/int, so a literal `None` would corrupt `model_copy`. Secret
    fields are encrypted on the way to storage; see secret_store.py.

    `configured_by_user_id` records who saved last; automations imported from
    git are created as that user (see loop.py).

    Changing the repo URL, branch or token clears the auth-failure backoff, so
    a corrected config is retried on its normal interval rather than waiting
    out a backoff earned by the old one.

    Setting, changing or clearing the encryption key marks every one of the
    remote's synced automations dirty. The export only rewrites dirty ones, so
    otherwise the repo kept each file in whatever form it was last written --
    readable after a key was set -- until that automation happened to change.
    """
    overrides = _decode_overrides(remote)
    before = {key: overrides.get(key) for key in _AUTH_BACKOFF_RESET_FIELDS}
    base = base_settings_for(remote)
    key_before = _merge(base, overrides).git_sync_encryption_key
    for key, value in updates.items():
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    if any(overrides.get(key) != before[key] for key in _AUTH_BACKOFF_RESET_FIELDS):
        remote.consecutive_auth_failures = 0
    if _merge(base, overrides).git_sync_encryption_key != key_before:
        await session.execute(
            update(AutomationGitSyncState).where(states_of(remote)).values(dirty=True)
        )
    remote.overrides = json.dumps(encrypt_secret_fields(overrides))
    if configured_by_user_id is not None:
        remote.configured_by_user_id = configured_by_user_id


async def apply_git_sync_config_override(
    session: AsyncSession,
    org_id: uuid.UUID,
    updates: dict[str, Any],
    *,
    configured_by_user_id: uuid.UUID,
) -> AutomationGitSyncRemote:
    """`apply_remote_config_override` on the org's default remote, which the
    legacy `PUT /v1/git-sync/config` creates on first save."""
    remote = await get_or_create_default_remote(session, org_id)
    await apply_remote_config_override(
        session, remote, updates, configured_by_user_id=configured_by_user_id
    )
    return remote


# --- Repository identity --------------------------------------------------------


def _normalize_repo_url(url: str) -> str:
    normalized = url.strip().lower().rstrip("/")
    return normalized.removesuffix(".git")


def _repo_identity(git_settings: GitSyncSettings) -> tuple[str, str, str]:
    return (
        _normalize_repo_url(git_settings.git_sync_repo_url),
        git_settings.git_sync_branch.strip(),
        git_settings.git_sync_path.strip().strip("/"),
    )


async def lock_repo_identity(session: AsyncSession, candidate: GitSyncSettings) -> None:
    """Serialise config writes that name the same repository, branch and path.

    `find_remote_using_repo` followed by a write is a check and then a write,
    so two writers saving the same repo at once could both pass the check and
    both keep it. Row locks can't close that gap: the other row may not exist
    yet. A transaction-scoped advisory lock on the repo identity holds the
    second writer until the first commits, so its check sees the committed
    row. Released by the caller's commit. SQLite runs single-process and
    skips it (the same pattern as `conversations.py`).
    """
    if using_sqlite():
        return
    digest = hashlib.blake2b(
        "/".join(_repo_identity(candidate)).encode(), digest_size=8
    ).digest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)").bindparams(
            key=int.from_bytes(digest, "big", signed=True)
        )
    )


async def find_remote_using_repo(
    session: AsyncSession,
    candidate: GitSyncSettings,
    *,
    exclude_remote_id: uuid.UUID | None,
) -> AutomationGitSyncRemote | None:
    """Another remote -- in any org -- already syncing `candidate`'s
    repository, branch and path, if any.

    Reads the plaintext identity fields straight from each blob, so no secret
    is unwrapped for a comparison. The table has one row per configured
    remote, so a scan is fine.
    """
    wanted = _repo_identity(candidate)
    rows = (await session.execute(select(AutomationGitSyncRemote))).scalars().all()
    for row in rows:
        if row.id == exclude_remote_id:
            continue
        raw = json.loads(row.overrides) if row.overrides else {}
        overrides = {
            key: raw[key]
            for key in _REPO_IDENTITY_FIELDS
            if isinstance(raw.get(key), str)
        }
        other = _merge(base_settings_for(row), overrides)
        if other.git_sync_repo_url and _repo_identity(other) == wanted:
            return row
    return None
