"""Mint a Supabase role token for the compute node.

The RPi authenticates as the ``shield_rpi`` Postgres role rather than using the
service-role key. That distinction is the whole reason the RLS policies in
``supabase/migrations/20261003090100_shield_roles_grants.sql`` mean anything: a
service-role key bypasses row level security entirely, so an RPi holding one
could insert events, read every device token and update any row, and none of the
policies would stop it.

PostgREST derives the Postgres role from a JWT, not from an ``apikey`` string, so
"a separate key" has to be a signed JWT whose ``role`` claim names the role. That
is what this mints.

Standard library only. ``PyJWT`` would be a dependency for forty lines of
base64 and HMAC, and this runs once.

    python tools/mint_role_token.py --role shield_rpi
    python tools/mint_role_token.py --role shield_rpi --print-env
    python tools/mint_role_token.py --role shield_rpi --ttl 31536000

Needs ``SUPABASE_JWT_SECRET``, the *JWT secret* from Supabase -> Settings -> API.
It is NOT the anon key, NOT the service-role key, and NOT ``WEBHOOK_SECRET``.
Writing it to ``.env`` is fine - ``.env`` is gitignored - but it is the single
most powerful credential in the project, because minting a token for
``service_role`` with it would bypass every policy.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A role is not invented here. It must already exist, created by
#: 20261003090100_shield_roles_grants.sql, so this script cannot mint a token for
#: a role that has no grants by accident.
KNOWN_ROLES = ("shield_rpi",)

#: PostgREST reads `role` and `exp`. `aud` is not required by PostgREST but is
#: conventional and harmless.
AUDIENCE = "authenticated"


class MintError(RuntimeError):
    """Raised for anything the operator has to fix."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_env(path: Path) -> dict[str, str]:
    """Read ``.env`` into a dict without mutating the process environment."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        # Only strip a trailing comment when whitespace introduces it, so a
        # '#' inside a base64 or JSON value survives.
        values[name.strip()] = value.split(" #", 1)[0].strip()
    return values


def mint(role: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    """Return ``(token, expires_at)`` for ``role``."""
    issued_at = int(time.time())
    expires_at = issued_at + ttl_seconds

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(
        json.dumps(
            {"role": role, "aud": AUDIENCE, "iat": issued_at, "exp": expires_at},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()

    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{b64url(signature)}", expires_at


def verify(token: str, secret: str) -> dict:
    """Decode and check the signature. Refuses to trust its own output blindly."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise MintError(f"token is not a three-part JWT: {exc}") from exc

    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256)
    # Constant-time: a byte-by-byte == leaks a matching prefix through timing.
    if not hmac.compare_digest(expected.digest(), base64.urlsafe_b64decode(signature_b64 + "==")):
        raise MintError("signature does not verify against the supplied secret")

    padding = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint a Supabase role JWT for the S.H.I.E.L.D compute node."
    )
    parser.add_argument("--role", default="shield_rpi", choices=KNOWN_ROLES)
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument(
        "--ttl", type=int, default=60 * 60 * 24 * 90, help="token lifetime in seconds"
    )
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="print the .env line to paste instead of just the token",
    )
    args = parser.parse_args(argv)

    if args.ttl < 60:
        print("MintError: --ttl below 60s will expire mid-run", file=sys.stderr)
        return 2

    # The environment wins over .env, matching how the service reads its own
    # config: systemd EnvironmentFile and an exported variable both override.
    secret = os.environ.get("SUPABASE_JWT_SECRET") or load_env(Path(args.env_file)).get(
        "SUPABASE_JWT_SECRET", ""
    )
    if not secret:
        print(
            "MintError: SUPABASE_JWT_SECRET is not set.\n"
            "  It is the JWT secret from Supabase -> Project Settings -> API,\n"
            "  not the anon key and not the service-role key. Add it to .env.",
            file=sys.stderr,
        )
        return 2

    token, expires_at = mint(args.role, secret, args.ttl)

    # Never print a token that has not been verified, and never print one
    # claiming a role that migration 20261003090100 did not create.
    claims = verify(token, secret)
    if claims.get("role") != args.role:
        print(f"MintError: token carries role {claims.get('role')!r}", file=sys.stderr)
        return 2

    if args.print_env:
        print(f"# minted by tools/mint_role_token.py, expires {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at))}")
        print(f"# role={args.role} - NOT service_role, so RLS still applies")
        print(f"SUPABASE_RPI_TOKEN={token}")
    else:
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())