"""Repository URL handling shared by the configuration and the rotation.

Its own module because both ``config.py`` and ``rotation.py`` need it and
neither should import the other.
"""

import re
from urllib.parse import urlsplit, urlunsplit


def strip_credentials(url: str) -> str:
    """Drop any user:password embedded in a repository URL.

    A cycle's repository URL is stored in history and shown in the dashboard,
    and a run clones with a token the agent server holds rather than one
    written into the URL. Either way the credential has no business being
    copied out of the Git Sync configuration and into another table.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.hostname or "@" not in parts.netloc:
        return url
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def normalise_repository_url(url: str | None) -> str | None:
    """A repository url reduced to the repository it names.

    The spellings arrive from different places and mean the same thing: a
    git-sync remote stores what the user pasted, a hand-written entry whatever
    they typed, and a credentialed url carries a token in its authority. None
    of those differences is a different repository.

    ``None`` when there is nothing to read, so a caller can fall back.
    """
    text = (url or "").strip()
    if not text:
        return None
    text = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", text)
    authority, slash, path = text.partition("/")
    # `user@host`, or `token:secret@host`: credentials identify the caller,
    # never the repository.
    authority = authority.rpartition("@")[2]
    if ":" in authority:
        host, _, after = authority.partition(":")
        # `host:owner/repo` is scp-style and the colon is the path separator;
        # `host:22` is a port and says nothing about which repository it is.
        authority = host if after.isdigit() else f"{host}/{after}"
    text = authority + slash + path
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if "/" not in text:
        # Not a url at all: a repository is a host and a path, so a bare name
        # like `HAZARD` has nothing to normalise. Returning `hazard` here
        # would hand it a url-shaped identity that no real repository can
        # ever equal — the caller's label fallback is the honest answer, and
        # it says plainly that all anybody had was a name.
        return None
    # Lowercased: the forges this talks to treat a host and an owner/name
    # case-insensitively, and two cases of one name sharing an allowance is
    # the right way round to be wrong.
    return text.lower() or None
