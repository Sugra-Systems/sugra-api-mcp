"""Static checks for the Docker deployment artifacts (issue #54).

No docker daemon involved: these tests read the files as text/YAML so CI can
guard the container recipe on every matrix leg without Docker installed.

Two contracts matter:
- The image must start and answer MCP introspection over stdio with NO
  environment variables set (evaluation runners build the Dockerfile and
  speak stdio to the container), so the Dockerfile must not bake
  SUGRA_API_KEY as ENV or ARG and the entrypoint must default to stdio.
- The compose file is the one-command HTTP deployment: port 8001, env
  passthrough for the documented variables, healthcheck on /health.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

PASSTHROUGH_ENV_VARS = [
    "SUGRA_API_KEY",
    "SUGRA_API_BASE",
    "SUGRA_TIMEOUT",
    "SUGRA_MCP_ALLOWED_ORIGINS",
    "SUGRA_MCP_ALLOWED_HOSTS",
]


def _dockerfile_instructions() -> list[str]:
    """Logical Dockerfile instructions with comments stripped and line
    continuations joined."""
    instructions: list[str] = []
    logical = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        logical += stripped
        if logical.endswith("\\"):
            logical = logical[:-1] + " "
            continue
        instructions.append(logical)
        logical = ""
    if logical:
        instructions.append(logical)
    return instructions


def _compose_service() -> dict:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data["services"]["sugra-api-mcp"]


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.is_file()


def test_dockerfile_pins_expected_base_image_family() -> None:
    froms = [
        instr.split()[1] for instr in _dockerfile_instructions() if instr.startswith("FROM ")
    ]
    assert froms == ["python:3.13-slim"]


def test_dockerfile_builds_package_from_repo_source_with_http_extra() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY sugra_api_mcp" in content, "image must build from the repo source tree"
    assert re.search(r"pip install [^\n]*\.\[http\]", content), (
        "pip install must target the local package with the [http] extra"
    )
    assert "--no-cache-dir" in content


def test_dockerfile_runs_as_non_root_user() -> None:
    users = [
        instr.split()[1] for instr in _dockerfile_instructions() if instr.startswith("USER ")
    ]
    assert users, "Dockerfile must switch to a non-root user"
    assert users[-1] not in {"root", "0"}


def test_dockerfile_entrypoint_defaults_to_stdio() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["sugra-api-mcp"]' in content
    # Empty CMD: no default arguments, and `sugra-api-mcp` with no arguments
    # runs the stdio transport (see sugra_api_mcp/__main__.py).
    assert "CMD []" in content


def test_dockerfile_bakes_no_api_key_as_env_or_arg() -> None:
    for instr in _dockerfile_instructions():
        if instr.startswith(("ENV ", "ARG ")):
            assert "SUGRA_API_KEY" not in instr, (
                "the image must start without SUGRA_API_KEY; never declare it in the Dockerfile"
            )


def test_compose_parses_with_expected_service_keys() -> None:
    service = _compose_service()
    for key in ("build", "command", "ports", "environment", "healthcheck"):
        assert key in service, f"docker-compose service is missing the {key} key"


def test_compose_runs_http_transport_on_8001() -> None:
    service = _compose_service()
    command = [str(part) for part in service["command"]]
    assert "streamable-http" in command
    assert "0.0.0.0" in command
    assert "8001" in command
    assert "8001:8001" in [str(port) for port in service["ports"]]


def test_compose_environment_is_bare_name_passthrough() -> None:
    env = [str(entry) for entry in _compose_service()["environment"]]
    for name in PASSTHROUGH_ENV_VARS:
        assert name in env, f"{name} must pass through from the shell environment"
    for entry in env:
        assert "=" not in entry, f"no baked values in compose environment: {entry}"


def test_compose_healthcheck_hits_health_route() -> None:
    check = _compose_service()["healthcheck"]["test"]
    joined = " ".join(str(part) for part in check)
    assert "curl" in joined
    assert "http://localhost:8001/health" in joined


def test_dockerignore_covers_repo_noise() -> None:
    entries = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in (".git", ".claude", ".venv", "tests", "evals", "dist"):
        assert required in entries, f".dockerignore must exclude {required}"
