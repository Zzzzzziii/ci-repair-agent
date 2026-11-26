import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

from ci_repair.schemas import ProjectProfile
from ci_repair.workspace import repository_path, within


DEFAULT_CONFIG = ".ci-repair.toml"
CONFIG_KEYS = {"cwd", "python", "source_paths", "import_modules", "pytest_args", "regression", "timeout", "embedding_model"}
PROJECT_PROBE = """
import sys
if sys.path and sys.path[0] == "":
    sys.path.pop(0)

import importlib.util
import json
import os
import pytest

sys.path.insert(0, os.getcwd())
origins = {}
for name in sys.argv[1:]:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    origins[name] = None if spec is None else spec.origin or list(spec.submodule_search_locations or ())
print(json.dumps({"python_version": sys.version.split()[0], "pytest_version": pytest.__version__,
                  "imports": origins}))
"""


def load_project_config(repository: Path, config: Path | None) -> tuple[Path | None, dict]:
    path = config.absolute() if config is not None else repository / DEFAULT_CONFIG
    try:
        with path.open("rb") as stream:
            settings = tomllib.load(stream)
    except FileNotFoundError:
        if config is not None:
            raise
        return None, {}
    except (ValueError, OSError) as error:
        raise ValueError(f"Could not read {path}: {error}") from error

    if unknown := sorted(settings.keys() - CONFIG_KEYS):
        raise ValueError(f"Unknown project settings in {path}: {', '.join(unknown)}")
    for key in ("cwd", "python", "embedding_model"):
        if key in settings and (not isinstance(settings[key], str) or not settings[key].strip()):
            raise ValueError(f"{key} in {path} must be a nonempty string")
    for key in ("source_paths", "import_modules", "pytest_args", "regression"):
        if key in settings and (
            not isinstance(settings[key], list)
            or any(not isinstance(value, str) or not value.strip() for value in settings[key])
        ):
            raise ValueError(f"{key} in {path} must be an array of nonempty strings")
    timeout = settings.get("timeout", 120)
    if type(timeout) not in (int, float) or not 0 < timeout <= sys.float_info.max:
        raise ValueError(f"timeout in {path} must be a positive finite number")
    return path, settings


def validate_selector(directory: Path, selector: str) -> None:
    path = selector.partition("::")[0].replace("\\", "/")
    if not path or path.startswith("-") or Path(path).is_absolute() or Path(path).drive:
        raise ValueError(f"Expected a pytest selector relative to --cwd: {selector}")
    within(directory, path)


def resolve_project_profile(
    repo: Path,
    config: Path | None = None,
    *,
    cwd: str | None = None,
    python: str | None = None,
    source_paths: list[str] | None = None,
    import_modules: list[str] | None = None,
    pytest_args: list[str] | None = None,
    regression: list[str] | None = None,
    timeout: float | None = None,
) -> ProjectProfile:
    repository = repository_path(repo)
    config_path, settings = load_project_config(repository, config)
    directory = within(repository, cwd if cwd is not None else settings.get("cwd", "."))

    interpreter = python if python is not None else settings.get("python", sys.executable)
    if python is None and "python" in settings and ("/" in interpreter or "\\" in interpreter):
        interpreter = str(repository / Path(interpreter.replace("\\", "/")))
    executable = shutil.which(interpreter)
    if executable is None:
        raise ValueError(f"Python interpreter not found: {interpreter}")

    selectors = regression if regression is not None else settings.get("regression", [])
    for selector in selectors:
        validate_selector(directory, selector)
    paths = source_paths if source_paths is not None else settings.get("source_paths", [])
    roots = [within(repository, path.replace("\\", "/")).relative_to(repository).as_posix() for path in paths]
    modules = import_modules if import_modules is not None else settings.get("import_modules", [])
    for name in modules:
        if not all(part.isidentifier() for part in name.split(".")):
            raise ValueError(f"Expected a dotted Python module name: {name}")
    options = pytest_args if pytest_args is not None else settings.get("pytest_args", [])
    if "--" in options:
        raise ValueError("pytest_args must not contain the -- selector separator")
    seconds = timeout if timeout is not None else settings.get("timeout", 120)
    if not 0 < seconds <= sys.float_info.max:
        raise ValueError("--timeout must be a positive finite number")
    return {
        "repository": str(repository),
        "project_config": str(config_path) if config_path is not None else None,
        "cwd": directory.relative_to(repository).as_posix(),
        "python": str(Path(executable).absolute()),
        "source_paths": roots,
        "import_modules": list(dict.fromkeys(modules)),
        "pytest_args": options,
        "regression": selectors,
        "timeout": seconds,
        "embedding_model": settings.get("embedding_model"),
    }


def prepare_candidate_environment(repository: Path, source_paths: list[str]) -> dict[str, str] | None:
    if not source_paths:
        return None
    roots = [within(repository, path) for path in source_paths]
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Source directory not found: {root}")
        if os.pathsep in str(root):
            raise ValueError(f"Source path contains the PYTHONPATH separator: {root}")
    env = os.environ.copy()
    paths = list(map(str, roots))
    if previous := env.get("PYTHONPATH"):
        paths.append(previous)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def check_project(profile: ProjectProfile) -> dict:
    repository = Path(profile["repository"])
    directory = within(repository, profile["cwd"])
    if not directory.is_dir():
        raise ValueError(f"Project directory not found: {directory}")
    probe = subprocess.run(
        [profile["python"], "-B", "-X", "utf8", "-c", PROJECT_PROBE, *profile["import_modules"]],
        cwd=directory, env=prepare_candidate_environment(repository, profile["source_paths"]),
        capture_output=True, encoding="utf-8", errors="replace", timeout=min(profile["timeout"], 15),
    )
    if probe.returncode or not probe.stdout.strip():
        detail = probe.stderr.strip().splitlines()
        raise ValueError(f"Could not check pytest with {profile['python']}: {detail[-1] if detail else probe.returncode}")
    observed = json.loads(probe.stdout.strip().splitlines()[-1])
    missing = [name for name, origin in observed["imports"].items() if origin is None]
    return {**profile, **observed, "status": "blocked" if missing else "ready", "missing_imports": missing}
