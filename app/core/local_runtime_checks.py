from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit


DiagnosticStatus = Literal["ok", "warning", "error"]

MIN_PYTHON_VERSION = (3, 11)
CONFIG_KEYS = (
    "APP_NAME",
    "ENVIRONMENT",
    "LOG_LEVEL",
    "DATABASE_URL",
    "SQL_ECHO",
    "TELEGRAM_ADMIN_ENABLED",
    "TELEGRAM_COMMANDS_ENABLED",
    "TELEGRAM_ADMIN_REPORTS_ENABLED",
    "TELEGRAM_DRY_RUN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ADMIN_CHAT_ID",
    "TELEGRAM_PUBLIC_CHANNEL_ID",
    "TELEGRAM_VIP_CHANNEL_ID",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_PORT",
)
POSTGRES_CONFIG_KEYS = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT")


@dataclass(frozen=True)
class RuntimeDiagnostic:
    name: str
    status: DiagnosticStatus
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def check_python_version(version_info: Sequence[int] | None = None) -> RuntimeDiagnostic:
    version = tuple(version_info or sys.version_info[:3])
    version_text = ".".join(str(part) for part in version[:3])
    required_text = ".".join(str(part) for part in MIN_PYTHON_VERSION)
    if version[:2] < MIN_PYTHON_VERSION:
        return RuntimeDiagnostic(
            name="python_version",
            status="error",
            message=f"Python {required_text}+ is required; found {version_text}.",
            details={"version": version_text, "required": f"{required_text}+"},
        )
    return RuntimeDiagnostic(
        name="python_version",
        status="ok",
        message=f"Python {version_text} meets the {required_text}+ requirement.",
        details={"version": version_text, "required": f"{required_text}+"},
    )


def check_virtual_environment(
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
    real_prefix: str | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeDiagnostic:
    process_env = os.environ if env is None else env
    runtime_prefix = prefix or sys.prefix
    runtime_base_prefix = base_prefix or getattr(sys, "base_prefix", runtime_prefix)
    runtime_real_prefix = real_prefix if real_prefix is not None else getattr(sys, "real_prefix", None)
    virtual_env = process_env.get("VIRTUAL_ENV")
    is_venv = bool(virtual_env or runtime_prefix != runtime_base_prefix or runtime_real_prefix)

    if not is_venv:
        return RuntimeDiagnostic(
            name="virtual_environment",
            status="warning",
            message="No active virtual environment was detected.",
            details={"active": False},
        )
    return RuntimeDiagnostic(
        name="virtual_environment",
        status="ok",
        message="A virtual environment is active.",
        details={"active": True, "path": virtual_env or runtime_prefix},
    )


def check_directory_writable(path: Path, *, name: str, label: str) -> RuntimeDiagnostic:
    try:
        if path.exists() and not path.is_dir():
            return RuntimeDiagnostic(
                name=name,
                status="error",
                message=f"{label} is not a directory: {path}",
                details={"path": str(path), "reason": "not_directory"},
            )

        path.mkdir(parents=True, exist_ok=True)
        probe_path = path / ".runtime_check_write_test"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
    except OSError as exc:
        return RuntimeDiagnostic(
            name=name,
            status="error",
            message=f"{label} is not writable: {exc}",
            details={"path": str(path), "error": type(exc).__name__},
        )

    return RuntimeDiagnostic(
        name=name,
        status="ok",
        message=f"{label} is writable.",
        details={"path": str(path)},
    )


def check_pytest_temp_writable(project_root: Path) -> RuntimeDiagnostic:
    return check_directory_writable(
        project_root / ".pytest_tmp",
        name="pytest_temp",
        label="Project-local pytest temp directory",
    )


def check_pytest_cache_writable(project_root: Path) -> RuntimeDiagnostic:
    return check_directory_writable(
        project_root / ".pytest_tmp" / "cache",
        name="pytest_cache",
        label="Project-local pytest cache directory",
    )


def check_legacy_pytest_cache_writable(project_root: Path) -> RuntimeDiagnostic:
    path = project_root / ".pytest_cache"
    if not path.exists():
        return RuntimeDiagnostic(
            name="legacy_pytest_cache",
            status="ok",
            message="Legacy .pytest_cache is absent; pytest uses .pytest_tmp/cache.",
            details={"path": str(path), "present": False},
        )

    diagnostic = check_directory_writable(
        path,
        name="legacy_pytest_cache",
        label="Legacy .pytest_cache directory",
    )
    if diagnostic.status == "error":
        return RuntimeDiagnostic(
            name="legacy_pytest_cache",
            status="warning",
            message=f"{diagnostic.message} Pytest is configured to use .pytest_tmp/cache instead.",
            details=diagnostic.details,
        )
    return RuntimeDiagnostic(
        name="legacy_pytest_cache",
        status="ok",
        message="Legacy .pytest_cache is writable, though pytest uses .pytest_tmp/cache.",
        details=diagnostic.details,
    )


def check_env_file_presence(project_root: Path) -> RuntimeDiagnostic:
    env_path = project_root / ".env"
    if env_path.exists():
        return RuntimeDiagnostic(
            name="env_file",
            status="ok",
            message=".env is present. Values were not printed.",
            details={"path": str(env_path), "present": True},
        )
    return RuntimeDiagnostic(
        name="env_file",
        status="warning",
        message=".env is missing. Copy .env.example to .env for local configuration.",
        details={"path": str(env_path), "present": False},
    )


def read_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_runtime_env(project_root: Path, process_env: Mapping[str, str] | None = None) -> dict[str, str]:
    values = read_env_file(project_root / ".env")
    values.update(dict(os.environ if process_env is None else process_env))
    return values


def config_key_states(values: Mapping[str, str], keys: Iterable[str] = CONFIG_KEYS) -> dict[str, str]:
    states: dict[str, str] = {}
    for key in keys:
        if key not in values:
            states[key] = "missing"
        elif str(values[key]).strip():
            states[key] = "set"
        else:
            states[key] = "empty"
    return states


def check_masked_config_keys(values: Mapping[str, str]) -> RuntimeDiagnostic:
    return RuntimeDiagnostic(
        name="config_keys",
        status="ok",
        message="Configuration keys were inspected without printing raw values.",
        details={"keys": config_key_states(values)},
    )


def mask_database_url(value: str | None) -> str:
    if value is None:
        return "missing"
    if not value.strip():
        return "empty"

    try:
        parts = urlsplit(value)
    except ValueError:
        return "<set>"

    if not parts.scheme:
        return "<set>"

    username = quote(parts.username or "", safe="")
    password = ":***" if parts.password is not None else ""
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port = ""
    try:
        if parts.port is not None:
            port = f":{parts.port}"
    except ValueError:
        port = ""

    auth = f"{username}{password}@" if username or password else ""
    query = "redacted=true" if parts.query else ""
    return urlunsplit((parts.scheme, f"{auth}{host}{port}", parts.path, query, ""))


def check_postgresql_config(values: Mapping[str, str]) -> RuntimeDiagnostic:
    database_url = values.get("DATABASE_URL")
    if database_url and database_url.strip():
        return RuntimeDiagnostic(
            name="postgresql_config",
            status="ok",
            message="PostgreSQL configuration is present via DATABASE_URL. Connectivity was not checked.",
            details={
                "source": "DATABASE_URL",
                "database_url": mask_database_url(database_url),
                "connectivity": "not_checked",
            },
        )

    key_states = config_key_states(values, POSTGRES_CONFIG_KEYS)
    required_missing = [key for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PORT") if key_states[key] != "set"]
    if not required_missing:
        return RuntimeDiagnostic(
            name="postgresql_config",
            status="ok",
            message="PostgreSQL environment keys are present. Connectivity was not checked.",
            details={"source": "POSTGRES_*", "keys": key_states, "connectivity": "not_checked"},
        )

    return RuntimeDiagnostic(
        name="postgresql_config",
        status="warning",
        message="PostgreSQL config is incomplete. Set DATABASE_URL or POSTGRES_* keys before database-backed workflows.",
        details={"missing": required_missing, "keys": key_states, "connectivity": "not_checked"},
    )


def check_docker_readiness(
    *,
    runner: SubprocessRunner = subprocess.run,
    timeout_sec: float = 3.0,
) -> RuntimeDiagnostic:
    command = ["docker", "version", "--format", "{{json .}}"]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        return RuntimeDiagnostic(
            name="docker",
            status="warning",
            message="Docker CLI was not found. Install Docker Desktop if local PostgreSQL containers are needed.",
            details={"command": "docker version", "timeout_sec": timeout_sec},
        )
    except subprocess.TimeoutExpired:
        return RuntimeDiagnostic(
            name="docker",
            status="warning",
            message="Docker did not respond before the timeout. Start Docker Desktop or retry later.",
            details={"command": "docker version", "timeout_sec": timeout_sec},
        )
    except OSError as exc:
        return RuntimeDiagnostic(
            name="docker",
            status="warning",
            message=f"Docker readiness could not be checked: {exc}",
            details={"command": "docker version", "error": type(exc).__name__},
        )

    if result.returncode == 0:
        return RuntimeDiagnostic(
            name="docker",
            status="ok",
            message="Docker CLI and daemon responded.",
            details={"command": "docker version"},
        )

    output = _subprocess_summary(result.stderr or result.stdout)
    message = "Docker is not ready."
    if output:
        message = f"{message} {output}"
    return RuntimeDiagnostic(
        name="docker",
        status="warning",
        message=message,
        details={
            "command": "docker version",
            "returncode": result.returncode,
            "hint": "Start Docker Desktop or check local Docker permissions. This does not block non-Docker diagnostics.",
        },
    )


def _subprocess_summary(output: str, *, max_length: int = 240) -> str:
    summary = " ".join(output.strip().split())
    if len(summary) > max_length:
        return f"{summary[: max_length - 3]}..."
    return summary


def find_generated_artifacts(project_root: Path) -> tuple[str, ...]:
    artifacts: list[str] = []
    scan_output = project_root / "scan_output.json"
    if scan_output.exists():
        artifacts.append("scan_output.json")

    scan_runs_dir = project_root / "scan_runs"
    if scan_runs_dir.exists():
        for path in sorted(scan_runs_dir.glob("*.json")):
            artifacts.append(path.relative_to(project_root).as_posix())
    return tuple(artifacts)


def check_generated_artifact_hygiene(project_root: Path) -> RuntimeDiagnostic:
    artifacts = find_generated_artifacts(project_root)
    if not artifacts:
        return RuntimeDiagnostic(
            name="generated_artifacts",
            status="ok",
            message="No generated scan JSON artifacts were found.",
            details={"artifact_count": 0, "artifacts": []},
        )

    return RuntimeDiagnostic(
        name="generated_artifacts",
        status="warning",
        message="Generated scan JSON artifacts are present as local ignored files; keep them out of commits.",
        details={"artifact_count": len(artifacts), "artifacts": list(artifacts[:20])},
    )


def collect_local_diagnostics(
    project_root: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    docker_runner: SubprocessRunner = subprocess.run,
    docker_timeout_sec: float = 3.0,
) -> tuple[RuntimeDiagnostic, ...]:
    root = (project_root or Path.cwd()).resolve()
    process_env = os.environ if env is None else env
    runtime_env = load_runtime_env(root, process_env)

    return (
        check_python_version(),
        check_virtual_environment(env=process_env),
        check_pytest_temp_writable(root),
        check_pytest_cache_writable(root),
        check_legacy_pytest_cache_writable(root),
        check_env_file_presence(root),
        check_masked_config_keys(runtime_env),
        check_postgresql_config(runtime_env),
        check_docker_readiness(runner=docker_runner, timeout_sec=docker_timeout_sec),
        check_generated_artifact_hygiene(root),
    )


def diagnostics_to_dicts(diagnostics: Iterable[RuntimeDiagnostic]) -> list[dict[str, Any]]:
    return [diagnostic.to_dict() for diagnostic in diagnostics]


def has_hard_blockers(diagnostics: Iterable[RuntimeDiagnostic]) -> bool:
    return any(diagnostic.status == "error" for diagnostic in diagnostics)


def diagnostic_summary(diagnostics: Iterable[RuntimeDiagnostic]) -> dict[str, int]:
    counts = {"ok": 0, "warning": 0, "error": 0}
    for diagnostic in diagnostics:
        counts[diagnostic.status] += 1
    return counts
