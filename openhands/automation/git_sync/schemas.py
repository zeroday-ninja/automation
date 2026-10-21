"""Pydantic schemas for the git sync API."""

import uuid

from pydantic import BaseModel, Field, field_validator

from openhands.automation.config import normalize_git_sync_path
from openhands.automation.utils.time import UtcDatetime


class GitSyncStatusResponse(BaseModel):
    """One remote's configuration and last-sync state.

    Also the shape of the legacy single-repo `GET /v1/git-sync/status`, which
    reports the org's default remote (or the unconfigured defaults when the
    org has none).
    """

    enabled: bool
    repo_url: str
    branch: str
    path: str
    encryption_enabled: bool
    interval_seconds: int
    last_synced_commit: str | None
    last_synced_at: UtcDatetime | None
    last_error: str | None
    last_error_at: UtcDatetime | None
    dirty_count: int
    # A cycle reports its outcome only once it finishes, so without these a
    # caller can't tell "running" from "nothing happened".
    sync_in_progress: bool = False
    sync_started_at: UtcDatetime | None = None


class GitSyncRemoteResponse(GitSyncStatusResponse):
    """A remote as listed by `GET /v1/git-sync/remotes`: its status plus what
    identifies it among the org's other remotes."""

    id: uuid.UUID
    # Operator-chosen display name; blank means "show the repository".
    label: str
    # Where automations not assigned to any remote go.
    is_default: bool
    # Live automations assigned to this remote (for the default, also the
    # unassigned ones it will adopt on its next cycle).
    automation_count: int


class GitSyncRemotesResponse(BaseModel):
    remotes: list[GitSyncRemoteResponse]
    # Live automations assigned to no remote. Zero whenever the org has a
    # default remote and it has run a cycle since they were created.
    unassigned_count: int


class GitSyncTriggerResponse(BaseModel):
    """`triggered` is False when a cycle was already running -- that cycle
    picks up everything this one would have, so the trigger is a no-op."""

    triggered: bool


class GitSyncCheckResponse(BaseModel):
    """Result of the check endpoints.

    `ok` means reachability, not correctness: git could list the remote's
    branches with these credentials. A token without write scope still passes,
    and the encryption key is never exercised.

    `branch_exists` False with `ok` True is normal for a never-synced repo --
    the first cycle creates the branch.
    """

    ok: bool
    branch_exists: bool = False
    # git's own failure output, with any credentials in the URL redacted.
    detail: str | None = None


class GitSyncConfigUpdateRequest(BaseModel):
    """Partial update for a remote's runtime config overrides.

    Omitted fields are unchanged; an explicit `null` clears that field's
    override, reverting to the env default (`interval_seconds` has no env var
    and defaults to 0). On the legacy `PUT /v1/git-sync/config` this applies
    to the org's default remote, created on first save; setting a repo URL is
    what enables a remote's sync.

    `interval_seconds` is how often to sync automatically; 0 is manual-only.

    String fields are stripped, and a blank is treated as `null`. The UI sends
    `""` for a cleared text field, and storing that would wedge sync: both
    `git checkout -B ""` and `git add -A -- ""` are fatal.
    """

    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=0)
    repo_url: str | None = None
    branch: str | None = None
    path: str | None = None
    token: str | None = None
    encryption_key: str | None = None
    author_name: str | None = None
    author_email: str | None = None

    @field_validator(
        "repo_url",
        "branch",
        "token",
        "encryption_key",
        "author_name",
        "author_email",
        mode="after",
    )
    @classmethod
    def _blank_clears_the_override(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator("path", mode="after")
    @classmethod
    def _normalize_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        # Raises for a traversing path, which FastAPI turns into a 422 rather
        # than letting it reach `sync_root` and the export's rmtree.
        return normalize_git_sync_path(value)


class GitSyncRemoteUpdateRequest(GitSyncConfigUpdateRequest):
    """`PUT /v1/git-sync/remotes/{id}`: the config update plus the fields that
    identify the remote among the org's others.

    `is_default: true` makes it the org's default; `false` is refused, since
    an org gives up its default by choosing another one, not by unsetting it
    (automations created in between would have nowhere to go).
    """

    label: str | None = Field(default=None, max_length=255)
    is_default: bool | None = None

    @field_validator("label", mode="after")
    @classmethod
    def _strip_label(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class GitSyncRemoteCreateRequest(GitSyncRemoteUpdateRequest):
    """`POST /v1/git-sync/remotes`: a repository URL is what makes a remote,
    so it is required here where it is optional on an update."""

    repo_url: str = Field(min_length=1)

    @field_validator("repo_url", mode="after")
    @classmethod
    def _strip_repo_url(cls, value: str) -> str:
        return value.strip()


class GitSyncAssignment(BaseModel):
    """Which remote one of the org's live automations syncs to."""

    automation_id: uuid.UUID
    name: str
    # The remote it is assigned to; `None` means none yet.
    remote_id: uuid.UUID | None
    # The remote whose next cycle will export it: the assignment, else the
    # org's default remote, else `None` (it is not synced anywhere).
    effective_remote_id: uuid.UUID | None
    # Its directory name under the remote's path, once it has a state row.
    slug: str | None
    dirty: bool


class GitSyncAssignmentsResponse(BaseModel):
    automations: list[GitSyncAssignment]


class GitSyncAssignmentUpdateRequest(BaseModel):
    """`PUT /v1/git-sync/assignments/{automation_id}`: move the automation to
    `remote_id`. Its directory is removed from the previous remote's repo on
    that remote's next cycle and written to the new one's."""

    remote_id: uuid.UUID
