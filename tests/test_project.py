from pathlib import Path
import sys

import pytest

from ci_repair.project import resolve_project_profile
from ci_repair.workspace import git


def test_explicit_project_arguments_override_config(tmp_path):
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".ci-repair.toml").write_text('timeout = 5\n', encoding="utf-8")
    config = tmp_path / "other.toml"
    config.write_text(
        'cwd = "service"\npython = "missing-configured-python"\nsource_paths = ["service/src"]\n'
        'import_modules = ["shop.price"]\nregression = ["tests"]\ntimeout = 30\n', encoding="utf-8",
    )
    assert resolve_project_profile(tmp_path)["timeout"] == 5
    with pytest.raises(ValueError, match="Python interpreter not found"):
        resolve_project_profile(tmp_path, config)

    profile = resolve_project_profile(
        tmp_path, config, cwd=".", python=sys.executable, source_paths=["lib"],
        import_modules=["main", "main"], regression=["checks"], timeout=10,
    )
    assert profile["project_config"] == str(config)
    assert profile["cwd"] == "."
    assert Path(profile["python"]) == Path(sys.executable)
    assert profile["source_paths"] == ["lib"]
    assert profile["import_modules"] == ["main"]
    assert profile["regression"] == ["checks"]
    assert profile["timeout"] == 10


@pytest.mark.parametrize(("toml", "message"), [
    ('time_out = 10\n', "Unknown project settings"),
    ('timeout = true\n', "positive finite number"),
    ('source_paths = ["../outside"]\n', "escapes the target directory"),
    ('import_modules = ["../shop"]\n', "Expected a dotted Python module name"),
])
def test_invalid_project_config(tmp_path, toml, message):
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".ci-repair.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        resolve_project_profile(tmp_path)
