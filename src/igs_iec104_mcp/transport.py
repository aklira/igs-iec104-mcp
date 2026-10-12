"""The HTTP transport of the server: Streamable HTTP on the loopback, with one bearer
token (IMPLEMENTATION.md §9; task M4).

The stdio transport is the default and needs no token. The HTTP transport is for a
client on the same machine: it listens on the loopback only, and every request must
carry the token. A remote listener is not offered: it would need TLS and an
authorization server, which is a separate decision. The token is read from a file that
only its owner can read, outside the project directory; it is never printed, logged, or
returned in an error. The server does not run an authorization server, so the protected
resource metadata that the SDK publishes names the server itself as the issuer; a client
is given the token out of band.

The SDK checks the Host and Origin headers against the names of the listener (DNS
rebinding protection), and limits the size of each request body.
"""

from __future__ import annotations

import hmac
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

PATH = "/mcp"
TOKEN_CLIENT = "igs-iec104-mcp"
# The listeners the transport accepts: the IPv4 and IPv6 loopback, and its name.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
# A token of 32 printable characters or more, as `secrets.token_urlsafe(32)` gives.
MIN_TOKEN_CHARACTERS = 32
MAX_TOKEN_CHARACTERS = 256
TOKEN_PATTERN = re.compile(
    rf"[\x21-\x7e]{{{MIN_TOKEN_CHARACTERS},{MAX_TOKEN_CHARACTERS}}}"
)
_OTHERS = 0o077


class TokenError(ValueError):
    """The token file or the HTTP settings cannot be used. The message names what to
    change, and never shows the token."""


def validate_token(value: str) -> str:
    """The token itself: printable, without spaces, of a usable length."""
    if TOKEN_PATTERN.fullmatch(value) is None:
        raise TokenError(
            f"the token must be {MIN_TOKEN_CHARACTERS} to {MAX_TOKEN_CHARACTERS} "
            "printable characters with no space; generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
        )
    return value


def read_token(path: Path, *, project: Path) -> str:
    """The token of the file. The file is refused when it is a symbolic link, not a
    regular file, readable by others, or inside the project directory."""
    if path.resolve().is_relative_to(project.resolve()):
        raise TokenError(
            f"the token file {path} is inside the project directory: keep it outside, "
            "since the project is versioned"
        )
    try:
        status = path.lstat()
    except OSError as error:
        raise TokenError(
            f"the token file {path} cannot be read: {error.strerror}"
        ) from error
    if stat.S_ISLNK(status.st_mode):
        raise TokenError(
            f"the token file {path} is a symbolic link: name the file itself"
        )
    if not stat.S_ISREG(status.st_mode):
        raise TokenError(f"the token file {path} is not a regular file")
    if stat.S_IMODE(status.st_mode) & _OTHERS:
        raise TokenError(
            f"the token file {path} is readable by others: make it owner-only "
            "(chmod 600)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise TokenError(f"the token file {path} is not text") from error
    return validate_token(text.rstrip("\n"))


class StaticToken:
    """The one token the server accepts. The comparison takes the same time whatever
    the request sends, as far as the lengths agree."""

    def __init__(self, token: str) -> None:
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode("utf-8"), self._token):
            return None
        return AccessToken(token=token, client_id=TOKEN_CLIENT, scopes=[])


@dataclass(frozen=True)
class HttpSettings:
    """The HTTP listener: a loopback host, a port the caller chose, and the token."""

    host: str
    port: int
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            raise TokenError(
                f"the HTTP transport listens on the loopback only, not {self.host!r}: "
                f"use one of {', '.join(LOOPBACK_HOSTS)}"
            )
        if not 1 <= self.port <= 65535:
            raise TokenError(f"the port {self.port} is not a port: use 1 to 65535")
        validate_token(self.token)


def base_url(settings: HttpSettings) -> str:
    """The address of the listener, as a client writes it."""
    host = "[::1]" if settings.host == "::1" else settings.host
    return f"http://{host}:{settings.port}"


def server_options(settings: HttpSettings | None) -> dict[str, Any]:
    """The options of the server that the HTTP transport needs. Stdio needs none."""
    if settings is None:
        return {}
    base = base_url(settings)
    return {
        "token_verifier": StaticToken(settings.token),
        # The one token is the server's own, so there is no resource indicator to check:
        # the verifier accepts it or not, as a whole.
        "auth": AuthSettings(
            issuer_url=AnyHttpUrl(base),
            resource_server_url=AnyHttpUrl(f"{base}{PATH}"),
            validate_token_resource=False,
        ),
    }


def transport_security(settings: HttpSettings) -> TransportSecuritySettings:
    """The Host and Origin values the listener accepts: its own names, on its port."""
    names = {
        "127.0.0.1": ("127.0.0.1", "localhost"),
        "localhost": ("localhost", "127.0.0.1"),
        "::1": ("[::1]",),
    }[settings.host]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{name}:{settings.port}" for name in names],
        allowed_origins=[f"http://{name}:{settings.port}" for name in names],
    )
