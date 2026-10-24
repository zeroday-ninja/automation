"""Configuration of an improvement program.

The configuration is stored on the automation as
``preset_metadata["improve"]`` and read back on every claim, so editing a
program takes effect on its next run without regenerating its tarball. The
packaged code in ``presets/improve/`` therefore carries no configuration of
its own.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openhands.automation.improve.url import strip_credentials


# A program may rotate over every repository the org syncs, or over an
# explicit list. "git_sync" is the normal case: the user already told Git Sync
# which repositories matter, and a program should not need a second list that
# can fall out of step with it.
RepositorySource = Literal["git_sync", "explicit"]

# What a run can actually clone: `RepoSource` in the SDK accepts exactly these.
CLONEABLE_URL_PREFIXES = ("https://", "git@", "file://")

MAX_REPOSITORIES = 50
MAX_REVIEWERS = 4
MAX_BUILDERS = 4
DEFAULT_BRANCH_PREFIX = "improve/"
DEFAULT_MAX_CHANGED_FILES = 20


class ExplicitRepository(BaseModel):
    """A repository named directly by the program rather than by Git Sync."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=255)
    url: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "Clone URL. Any embedded username or password is dropped: a cycle "
            "clones with a credential the agent server holds."
        ),
    )
    ref: str | None = Field(default=None, max_length=255)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Only remote git URLs a run can clone, and never with a credential.

        An embedded username or password is dropped rather than rejected. It
        would otherwise be stored in the automation's metadata, exported to the
        Git Sync repository along with it, and shown in the dashboard — and
        rejecting it would echo the secret back in the validation error, which
        is how it would reach logs. A cycle clones with a credential the agent
        server holds, so this URL never needs to carry one and nothing is lost
        by removing it.
        """
        v = v.strip()
        if not v.startswith(CLONEABLE_URL_PREFIXES):
            # Deliberately the same set the SDK's RepoSource accepts, no
            # wider. A looser rule here only moves the failure from creation
            # time, where the user is looking, to 3am inside a cycle. `http://`
            # is refused rather than silently upgraded, and `git://` and
            # `ssh://` are not cloneable by a run at all.
            raise ValueError(
                "url must start with one of: " + ", ".join(CLONEABLE_URL_PREFIXES)
            )
        return strip_credentials(v)


class ImproveProgramConfig(BaseModel):
    """What one improvement program does on each of its runs."""

    model_config = ConfigDict(extra="forbid")

    builder_profile_id: uuid.UUID = Field(
        ...,
        description=(
            "Agent profile that researches and implements the improvement. "
            "Its runtime is the one the reviewers must differ from. When "
            "`rotate_builder` is set, this is the first builder in the rotation."
        ),
    )
    rotate_builder: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_BUILDERS,
        description=(
            "Further profiles to take turns building, after "
            "`builder_profile_id`. The point is not variety for its own sake: "
            "with one fixed builder, that runtime never reviews anything. "
            "Rotating the builder is what makes *each* runtime take a turn "
            "attacking another's work. Empty keeps one builder."
        ),
    )
    reviewer_profile_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_REVIEWERS,
        description=(
            "Agent profiles that review the builder's work adversarially. "
            "Leave empty to let each run pick every available runtime other "
            "than the builder's, so adding a second runtime to the server is "
            "enough to turn review on. Naming them is how to keep the review "
            "pool closed to the runtimes you chose; with `rotate_builder`, "
            "the turn's own builder is skipped rather than self-reviewing."
        ),
    )
    judge_profile_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Agent profile that attests deletions: that each removal is dead, "
            "replaced, or covered, naming where. A third role beside builder "
            "and reviewers, so its runtime and model are yours to pick. "
            "Without one, a deletion waits for a person instead of "
            "publishing on its author's word."
        ),
    )
    repository_source: RepositorySource = Field(
        default="git_sync",
        description=(
            "Where the rotation's repositories come from: the org's Git Sync "
            "remotes, or the explicit `repositories` list."
        ),
    )
    repositories: list[ExplicitRepository] = Field(
        default_factory=list,
        max_length=MAX_REPOSITORIES,
        description="The rotation when `repository_source` is 'explicit'.",
    )
    repository_labels: list[str] = Field(
        default_factory=list,
        max_length=MAX_REPOSITORIES,
        description=(
            "Optional filter when `repository_source` is 'git_sync': rotate "
            "only over remotes whose label is listed. Empty means all of them."
        ),
    )
    branch_prefix: str = Field(
        default=DEFAULT_BRANCH_PREFIX,
        min_length=1,
        max_length=64,
        description="Branch names the program may create and force-push.",
    )
    max_changed_files: int = Field(
        default=DEFAULT_MAX_CHANGED_FILES,
        ge=1,
        le=500,
        description=(
            "Upper bound on one cycle's change. A cycle that would exceed it "
            "must narrow its scope instead."
        ),
    )
    open_pr: bool = Field(
        default=True,
        description=(
            "Open a pull request for the cycle's branch. When false the branch "
            "is pushed and the cycle reports it without opening one."
        ),
    )
    focus: str | None = Field(
        default=None,
        max_length=4000,
        description=(
            "Extra standing guidance for this program, added to the repository's "
            "own PROJECT_IDENTITY.md rather than replacing it."
        ),
    )

    daily_cycles_per_repository: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "Cycles this program may take on one repository in a day. The "
            "service's own limit applies when this is unset, and the stricter "
            "of the two always wins — a program cannot raise its own ceiling."
        ),
    )

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, v: str) -> str:
        """Keep the prefix usable as the start of a git ref."""
        if v.startswith("/") or ".." in v or v.strip() != v:
            raise ValueError("branch_prefix must be a valid git ref prefix")
        forbidden = set(" ~^:?*[\\\x7f")
        if any(c in forbidden or ord(c) < 0x20 for c in v):
            raise ValueError("branch_prefix contains characters git forbids in a ref")
        return v

    @model_validator(mode="after")
    def validate_source(self) -> "ImproveProgramConfig":
        """Each source needs its own field, and neither accepts the other's."""
        if self.repository_source == "explicit":
            if not self.repositories:
                raise ValueError(
                    "repositories must list at least one repository when "
                    "repository_source is 'explicit'"
                )
            if self.repository_labels:
                raise ValueError(
                    "repository_labels filters Git Sync remotes and cannot be "
                    "combined with repository_source 'explicit'"
                )
        elif self.repositories:
            raise ValueError(
                "repositories is only used when repository_source is 'explicit'"
            )
        if (
            not self.rotate_builder
            and self.builder_profile_id in self.reviewer_profile_ids
        ):
            # Only with a fixed builder. Under a rotation the first builder is
            # a reviewer on every other turn, which is the point; the
            # rotation-aware check below is what guards that case.
            raise ValueError(
                "the builder profile cannot also be a reviewer: a runtime "
                "reviewing its own work is not independent review"
            )
        if self.builder_profile_id in self.rotate_builder:
            raise ValueError(
                "builder_profile_id is already the first builder; list only "
                "the profiles that follow it in rotate_builder"
            )
        if len(set(self.rotate_builder)) != len(self.rotate_builder):
            raise ValueError("rotate_builder contains a duplicate")
        if self.rotate_builder and self.reviewer_profile_ids:
            # Naming reviewers while the builder moves is allowed, and is the
            # only way to say "these runtimes, and no others, review each
            # other": leaving reviewers empty draws from every profile on the
            # server, so a local model added for something else starts
            # reviewing. A reviewer that is the current turn's builder is
            # skipped for that turn rather than asked to review itself, so the
            # one thing to refuse is a turn with nobody left to review it.
            unreviewed = [
                builder
                for builder in self.builder_rotation
                if not set(self.reviewer_profile_ids) - {builder}
            ]
            if unreviewed:
                raise ValueError(
                    "every builder in the rotation needs a reviewer that is "
                    "not itself; these would build unreviewed: "
                    + ", ".join(str(builder) for builder in unreviewed)
                )
        if len(set(self.reviewer_profile_ids)) != len(self.reviewer_profile_ids):
            raise ValueError("reviewer_profile_ids contains a duplicate")
        if self.judge_profile_id is not None and self.judge_profile_id in (
            self.builder_rotation
        ):
            # Every profile in the rotation builds on some turn, so a judge
            # drawn from it would eventually be signing off its own removals.
            # The service checks the runtime that actually signed as well --
            # this just stops the configuration from guaranteeing the clash.
            raise ValueError(
                "the judge cannot be a profile that also builds: a runtime "
                "attesting its own deletions is not independent attestation"
            )
        return self

    @property
    def builder_rotation(self) -> list[uuid.UUID]:
        """Every profile that takes a turn building, in order."""
        return [self.builder_profile_id, *self.rotate_builder]

    def to_metadata(self) -> dict[str, Any]:
        """Serialize for ``preset_metadata['improve']`` (JSON-safe)."""
        return self.model_dump(mode="json", exclude_none=False)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "ImproveProgramConfig":
        """Read back from an automation's ``preset_metadata``.

        Raises ``ValueError`` when the automation is not an improvement
        program, so callers can answer 404/409 rather than guessing a default
        configuration for an automation that never had one.
        """
        if not isinstance(metadata, dict):
            raise ValueError("automation has no preset metadata")
        raw = metadata.get("improve")
        if not isinstance(raw, dict):
            raise ValueError("automation is not an improvement program")
        return cls.model_validate(raw)
