"""Repository URL handling shared by the configuration and the rotation.

Its own module because both ``config.py`` and ``rotation.py`` need it and
neither should import the other.
"""

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
