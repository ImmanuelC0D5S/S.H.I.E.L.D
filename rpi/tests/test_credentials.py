"""Role separation and credential-hygiene guards.

These tests are about the project's *configuration*, not its behaviour, but the
behaviour they protect is the most severe class of bug available here: a
compute node holding a credential that bypasses row level security, or a private
key one ``git add .`` away from permanent history.

Both are silent. Neither shows up in a test run. Both are trivially prevented by
an assertion, which is what this file is.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHIELD_RPI = REPO / "rpi" / "shield_rpi"
MIGRATIONS = REPO / "supabase" / "migrations"


def python_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.py"))


def strip_docstrings(source: str) -> str:
    """Remove docstrings and comments so prose cannot satisfy a code check."""
    tree = ast.parse(source)
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))

    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) in docstring_nodes:
            lines[node.lineno - 1] = ""
    return "\n".join(lines)


# -- the RPi must not hold a service-role key -------------------------------


def test_no_rpi_module_knows_the_service_role_key():
    """A service-role key bypasses RLS entirely.

    Every policy in 20261003090100_shield_roles_grants.sql would become
    decorative, and the RPi could read every device token and rewrite any event.
    """
    forbidden = ("SUPABASE_SERVICE_KEY", "service_role", "sb_secret_")
    for path in python_files(SHIELD_RPI):
        code = strip_docstrings(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} references {token}"


def test_the_rpi_token_is_the_only_supabase_credential_config_accepts():
    """config.yaml must interpolate the role token, not the service key."""
    config = (SHIELD_RPI / "config.yaml").read_text(encoding="utf-8")
    supabase = config.split("supabase:", 1)[1]
    assert "${SUPABASE_RPI_TOKEN}" in supabase
    assert "SUPABASE_SERVICE_KEY" not in supabase


def test_mint_role_token_refuses_roles_that_bypass_rls():
    """The script must not be able to mint a service_role token.

    It holds the JWT secret, so an arbitrary role argument would be a privilege
    escalation path. The role list is a closed set for that reason.
    """
    source = (REPO / "tools" / "mint_role_token.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    known = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "KNOWN_ROLES":
                    known = ast.literal_eval(node.value)

    assert known is not None, "KNOWN_ROLES must be a literal so it can be checked here"
    assert "service_role" not in known
    assert "anon" not in known
    assert known == ("shield_rpi",)


def test_a_minted_token_carries_only_the_rpi_role():
    import sys

    sys.path.insert(0, str(REPO / "tools"))
    from mint_role_token import mint, verify

    token, _ = mint("shield_rpi", "unit-test-secret", 3600)
    claims = verify(token, "unit-test-secret")
    assert claims["role"] == "shield_rpi"
    assert set(claims) == {"role", "aud", "iat", "exp"}
    assert "service_role" not in json.dumps(claims)


# -- credentials must not be committable ------------------------------------


def test_gitignore_covers_every_credential_path():
    """These files existed untracked-but-unignored. One `git add .` and a
    Firebase admin private key is in history permanently."""
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    required = {
        ".env": r"^\.env$",
        "secrets/": r"^secrets/$",
        "google-services.json": r"google-services\.json",
        "firebase service account": r"firebase-adminsdk.*\.json$",
        "keystores": r"\.keystore$|\.jks$",
    }
    for label, pattern in required.items():
        assert any(
            re.search(pattern, line.strip()) for line in ignore
        ), f".gitignore does not cover {label} ({pattern})"


def test_android_build_output_is_ignored_at_every_depth():
    """`build/` matches nothing at `app/app/build/`.

    The Gradle root is `app/` and the module is `app/app/`, so the artifact
    directory is two levels down. A one-level pattern silently matches nothing and
    the first `git add app` commits ~2000 generated files, including the merged
    `processDebugGoogleServices/values.xml` that carries the Firebase API key.
    """
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(
        line.strip() in ("app/**/build/", "**/build/") for line in ignore
    ), "no pattern matches app/app/build/"

    build = REPO / "app" / "app" / "build"
    if not build.exists():
        pytest.skip("no Android build output present")

    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "app/"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")

    leaked = [line for line in result.stdout.splitlines() if "/build/" in line]
    assert not leaked, f"build output is not ignored: {leaked[:5]}"


def test_no_service_account_json_is_tracked_by_git():
    tracked = _git_ls_files()
    for path in tracked:
        lowered = path.lower()
        assert "firebase-adminsdk" not in lowered, f"{path} is tracked"
        assert not lowered.startswith("secrets/"), f"{path} is tracked"
        assert lowered != "app/app/google-services.json", f"{path} is tracked"
        assert lowered != ".env", f"{path} is tracked"


def _git_ls_files() -> list[str]:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# -- no hardcoded secrets in source -----------------------------------------


def test_no_long_base64ish_literal_in_the_rpi_package():
    """A 32+ character run of base64 alphabet is what a pasted key looks like."""
    pattern = re.compile(r"""["'][A-Za-z0-9+/=_-]{40,}["']""")
    for path in python_files(SHIELD_RPI):
        code = strip_docstrings(path.read_text(encoding="utf-8"))
        for match in pattern.finditer(code):
            raise AssertionError(f"{path.name} contains a suspicious literal: {match.group(0)[:24]}...")


def assigned_values(text: str) -> dict[str, str]:
    """Return ``{name: value}`` for every assignment, ignoring comments.

    Necessary because ``.env.example`` is *documented* with key prefixes such as
    ``sb_secret_...`` in comments. A test that grepped the raw text would fail on
    correct documentation and force the instructions to be deleted.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.split(" #", 1)[0].strip()
    return values


def test_env_example_carries_placeholders_not_values():
    values = assigned_values((REPO / ".env.example").read_text(encoding="utf-8"))

    for name, value in values.items():
        assert not value.startswith("sb_secret_"), f"{name} holds a real secret key"
        assert not value.startswith("sb_publishable_"), f"{name} holds a real publishable key"
        assert "BEGIN PRIVATE KEY" not in value, f"{name} holds a private key"
        assert not re.fullmatch(r"[0-9a-f]{64,}", value), f"{name} holds a long hex secret"

    # The non-secret values that legitimately ship filled in.
    assert values["SUPABASE_URL"] == "https://YOUR-PROJECT-REF.supabase.co"
    assert values["LINK_PORT"] == "8765"
    assert values["SHIELD_WIFI_SSID"] == "shield-net"

    # And the ones that must be filled in by the operator.
    assert values["SUPABASE_KEY"] == ""
    assert values["SUPABASE_SERVICE_KEY"] == ""
    assert values["WEBHOOK_SECRET"] == ""
    assert values["SHIELD_LINK_KEY"] == ""
    assert values["FCM_SERVICE_ACCOUNT_JSON"] == ""


# -- the edge function owns the credential ----------------------------------


def test_the_fcm_credential_is_read_from_the_environment_only():
    """supabase/functions/send-alert/index.ts reads FCM_SERVICE_ACCOUNT_JSON from
    Deno.env. It must not also read it from a file in the repository."""
    source = (REPO / "supabase" / "functions" / "send-alert" / "index.ts").read_text(
        encoding="utf-8"
    )
    assert "Deno.env.get('FCM_SERVICE_ACCOUNT_JSON')" in source
    assert "Deno.readTextFile" not in source
    assert "Deno.readFile" not in source


def test_the_edge_function_has_no_npm_imports():
    """Minimal dependencies: JWT signing is Web Crypto and the signed storage URL
    is a plain fetch, so cold start is just the Deno boot."""
    source = (REPO / "supabase" / "functions" / "send-alert" / "index.ts").read_text(
        encoding="utf-8"
    )
    for pattern in ("npm:", "jsr:", "https://esm.sh", "from \"jose\""):
        assert pattern not in source, f"send-alert must not import {pattern}"


# -- schema and code agree --------------------------------------------------


def test_the_supabase_project_ref_is_consistent():
    """Four copies of the project ref: .env, config.toml, the webhook trigger and
    this test. A mismatch is the first thing to check when "migrations applied
    but the phone hears nothing"."""
    config = (REPO / "supabase" / "config.toml").read_text(encoding="utf-8")
    match = re.search(r'project_id\s*=\s*"([a-z0-9-]+)"', config)
    assert match, "config.toml must declare project_id"
    ref = match.group(1)

    webhook = (MIGRATIONS / "20261003090200_send_alert_webhook.sql").read_text(encoding="utf-8")
    assert f"https://{ref}.supabase.co/functions/v1/send-alert" in webhook, (
        "the pg_net trigger URL does not match config.toml's project ref"
    )

    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "SUPABASE_URL=https://YOUR-PROJECT-REF.supabase.co" in env


def test_the_app_package_name_is_consistent():
    """FCM tokens are bound to the package name in google-services.json, and the
    Gradle applicationId has to match it exactly or the build fails."""
    services = REPO / "app" / "app" / "google-services.json"
    assert services.is_file(), "google-services.json is missing from app/app/"
    google = json.loads(services.read_text(encoding="utf-8"))
    package = google["client"][0]["client_info"]["android_client_info"]["package_name"]

    manifest = REPO / "app" / "app" / "src" / "main" / "AndroidManifest.xml"
    if not manifest.is_file():
        pytest.skip("Android app not scaffolded yet")
    assert package in manifest.read_text(encoding="utf-8"), (
        f"manifest applicationId does not match google-services.json's {package}"
    )

    build_file = REPO / "app" / "app" / "build.gradle.kts"
    assert build_file.is_file(), "app/app/build.gradle.kts is missing"
    assert f'"{package}"' in build_file.read_text(encoding="utf-8"), (
        f"build.gradle.kts applicationId != {package}"
    )


def test_the_backend_directory_stays_empty():
    """PROTOCOL.md 2.0.0 section 3 is retired; D-040 deleted the FastAPI service.

    The directory is kept as an empty tombstone rather than removed, because
    PROTOCOL.md and PLAN.md still reference it as the abandoned path and deleting
    the directory makes that reference look like an error. But a half-migrated
    backend is how a deployment ends up with two alert paths and a phone
    registered against the wrong one, so any content is a test failure.
    """
    backend = REPO / "backend"
    assert backend.is_dir(), "backend/ should exist as an empty tombstone, not vanish"

    tracked = [
        path
        for path in _git_ls_files()
        if path.startswith("backend/")
    ]
    assert not tracked, f"backend/ must stay empty; found {tracked}"

    on_disk = [p for p in backend.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert not on_disk, f"backend/ must stay empty; found {[str(p) for p in on_disk]}"


def test_no_rpi_module_imports_a_backend_package():
    """The one-sided-change guard: if `alerts.py` imported a backend client, the
    RPi would have two alert paths and the tests here would pass while the
    deployment chose the wrong one."""
    for path in python_files(SHIELD_RPI):
        code = strip_docstrings(path.read_text(encoding="utf-8"))
        assert "from backend" not in code, f"{path.name} still imports the retired backend"
        assert "import backend" not in code, f"{path.name} still imports the retired backend"


def test_the_alert_payload_fields_are_the_same_on_both_sides():
    """shield-app-prompt.md fixes six fields. The Edge Function builds them and
    the Kotlin app parses them; a rename on one side only means no alert ever
    displays a snapshot."""
    fields = {"event_id", "ts", "status", "confidence", "image_url", "needs_verdict"}

    function = (REPO / "supabase" / "functions" / "send-alert" / "index.ts").read_text(
        encoding="utf-8"
    )
    built = set(
        re.findall(r"^\s{4}(\w+):\s*(?:String\(|\w)", function, re.MULTILINE)
    )
    assert fields <= built, f"send-alert is missing payload fields: {fields - built}"

    parser = (
        REPO
        / "app"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "immanuelc0d5s"
        / "shield"
        / "data"
        / "AlertPayload.kt"
    )
    if not parser.is_file():
        pytest.skip("Android app not scaffolded yet")

    # The Kotlin side names the fields through KEY_* constants, so the check is
    # on those values rather than on call sites. Grepping for literal field names
    # would also pass if the constants existed but nothing read them.
    kotlin = parser.read_text(encoding="utf-8")
    declared = dict(re.findall(r'const val KEY_(\w+)\s*=\s*"(\w+)"', kotlin))

    assert len(declared) == 6, f"expected six KEY_ constants, found {sorted(declared)}"
    assert set(declared.values()) == fields, (
        f"AlertPayload's wire names are {sorted(declared.values())}, "
        f"expected {sorted(fields)}"
    )