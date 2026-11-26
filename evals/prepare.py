from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
from textwrap import indent
from time import perf_counter
from xml.etree import ElementTree
from zipfile import ZipFile

from ci_repair.workspace import git
from isolated import container_run


def git_bytes(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace")[-1200:])
    return result.stdout


def upstream(case, cache):
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (case.repository.split("/")[-1] + ".git")
    if not path.exists():
        subprocess.run(["git", "clone", "--bare", "https://github.com/" + case.repository + ".git", str(path)],
                       check=True, timeout=180)
    if git(path, "rev-parse", case.fix + "^").strip() != case.bad:
        raise ValueError("Upstream parent differs from the pinned failure")
    if git(path, "show", "-s", "--format=%cs", case.fix).strip() != case.date:
        raise ValueError("Upstream fix date differs from the manifest split date")
    return path


def extract(repo, revision, destination, *, package=None):
    args = ["-c", "core.autocrlf=false", "archive", "--format=zip", revision]
    executables = []
    if package:
        licenses = [name for name in git(repo, "ls-tree", "--name-only", revision).splitlines()
                    if name.startswith(("LICENSE", "LICENCE", "COPYING"))]
        args.extend(["--", package, *licenses])
    with ZipFile(BytesIO(git_bytes(repo, *args))) as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if info.is_dir() or name.is_absolute() or ".." in name.parts or "\\" in info.filename:
                continue
            if package and not (name.name.startswith(("LICENSE", "LICENCE", "COPYING")) or
                                (name.suffix == ".py" and not {"test", "tests"}.intersection(name.parts))):
                continue
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            if stat.S_IXUSR & (info.external_attr >> 16):
                target.chmod(0o755)
                executables.append(name.as_posix())
    return executables


def tests_source(case, checks):
    return "import pytest\n" + case.imports + "\n\n" + "\n".join(
        f"def test_{key}():\n" + indent(getattr(case, key), "    ") + "\n" for key in checks)


def prepare(case, cache, folder):
    src = upstream(case, cache)
    view = folder / "repo"
    view.mkdir(parents=True)
    executables = extract(src, case.bad, view, package=case.package)
    if case.package == "src/humanize" and not (view / case.package / "_version.py").exists():
        (view / case.package / "_version.py").write_text("__version__ = '0+benchmark'\n", encoding="utf-8", newline="\n")
    (view / "test_public.py").write_text(tests_source(case, ("target", "regression")), encoding="utf-8", newline="\n")
    (view / "pytest.ini").write_text("[pytest]\n", encoding="utf-8", newline="\n")
    (view / ".ci-repair.toml").write_text(f'repo_id = "{case.repository}"\nsource_paths = ["{case.source_root}"]\n',
                                         encoding="utf-8", newline="\n")
    git(view, "init", "--quiet")
    git(view, "config", "core.autocrlf", "false")
    git(view, "add", ".")
    if executables:
        git(view, "update-index", "--chmod=+x", "--", *executables)
    date = case.date + "T00:00:00+0000"
    subprocess.run(["git", "-C", str(view), "-c", "user.name=Benchmark", "-c",
                    "user.email=benchmark@example.invalid", "-c", "commit.gpgSign=false", "commit", "-qm", "Public baseline"],
                   check=True, env=dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date))
    paths = sorted({symbol.split(":")[0] for symbol in case.gold})
    patch = git_bytes(src, "diff", "--no-ext-diff", case.bad, case.fix, "--", *paths)
    if not patch:
        raise ValueError("Missing reference source change")
    return view, patch


def independent_tests(case, view, folder, patch=None, image=None):
    """Apply the candidate to a fresh export. The evaluator owns this directory."""
    tree = folder / "tree"
    tree.mkdir(parents=True)
    extract(view, "HEAD", tree)
    if patch is not None:
        # Use an independent Git root even when the output is inside the agent project.
        git(tree, "init", "--quiet")
        git(tree, "config", "core.autocrlf", "false")
        git(tree, "apply", "--whitespace=nowarn", "-", stdin=patch.decode("utf-8"))
    (tree / "test_private.py").write_text(tests_source(case, ("hidden",)), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tree / case.source_root), PYTEST_ADDOPTS="", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               PYTHONDONTWRITEBYTECODE="1", LANGUAGE="en", LC_ALL="C")
    evidence = folder / "evidence"
    evidence.mkdir()
    xml = evidence / "junit.xml"
    args = ["-m", "pytest", "-q", "--tb=short", "-c", "pytest.ini",
            "--junitxml", "/out/junit.xml" if image else str(xml), "test_public.py", "test_private.py"]
    started = perf_counter()
    if image:
        result = container_run(image, tree, evidence, args, folder / "pytest.log", folder / "stderr.log", 90)
        exit_code = result["exit_code"]
    else:
        run = subprocess.run([sys.executable, *args], cwd=tree, env=env, capture_output=True,
                             encoding="utf-8", errors="replace", timeout=90)
        (folder / "pytest.log").write_text(run.stdout + run.stderr, encoding="utf-8")
        exit_code = run.returncode
    statuses = {}
    public_failure = ""
    for node in ElementTree.parse(xml).iter("testcase"):
        status = next((tag for tag in ("error", "failure", "skipped") if node.find(tag) is not None), "passed")
        statuses[node.get("name")] = status
        if node.get("name") == "test_target" and status == "failure":
            public_failure = node.findtext("failure", "")
    return {"tests": statuses, "exit": exit_code, "seconds": round(perf_counter()-started, 4),
            "public_failure": public_failure, "log": str(folder / "pytest.log")}
