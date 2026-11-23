from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import subprocess
from uuid import uuid4


def git(repository: Path, *args: str, stdin: str | None = None) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *args],
        input=stdin.encode("utf-8") if stdin is not None else None,
        capture_output=True,
        timeout=30,
    )
    if process.returncode:
        raise ValueError(process.stderr.decode("utf-8", errors="replace").strip() or f"git {args[0]} failed")
    return process.stdout.decode("utf-8", errors="replace")


def repository_path(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip()).resolve()


def commit_id(repository: Path, ref: str) -> str:
    return git(repository, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()


def within(directory: Path, relative_path: str) -> Path:
    path = (directory / relative_path).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError(f"Path escapes the target directory: {relative_path}")
    return path


@contextmanager
def worktree(repository: Path, commit: str, scratch: Path):
    parent = scratch.resolve()
    tree = (parent / f"worktree-{uuid4().hex}").resolve()
    if not tree.is_relative_to(parent):
        raise ValueError("Invalid worktree path")
    git(repository, "worktree", "add", "--detach", str(tree), commit)
    try:
        yield tree
    finally:
        git(repository, "worktree", "remove", "--force", str(tree))


def protected_source(path: str, selectors: list[str]) -> bool:
    file = PurePosixPath(path)
    return (
        file.suffix != ".py"
        or any(part in {"tests", "test", ".github"} for part in file.parts)
        or file.name in {"conftest.py", "setup.py", "pytest.py"}
        or file.name.startswith("test_")
        or file.name.endswith("_test.py")
        or any(file == PurePosixPath(selector.partition("::")[0]) for selector in selectors)
    )


def apply_patch(tree: Path, patch: str, protected_selectors: list[str]) -> str:
    if not patch.strip() or len(patch) > 100_000:
        raise ValueError("Patch is empty or exceeds 100,000 characters")
    patch = patch.rstrip("\n") + "\n"
    git(tree, "apply", "--check", "--index", "-", stdin=patch)
    git(tree, "apply", "--index", "-", stdin=patch)
    changed = git(tree, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
    paths = list(filter(None, changed))
    if not paths:
        raise ValueError("Patch did not change tracked source files")
    if git(tree, "diff", "--summary", "--no-renames", "HEAD").strip():
        raise ValueError("Patch must edit existing regular files without changing their mode")
    for path in paths:
        target = within(tree, path)
        if protected_source(path, protected_selectors) or not target.is_file() or (tree / path).is_symlink():
            raise ValueError(f"Patch touches a protected or unsupported path: {path}")
    return git(tree, "-c", "core.quotePath=false", "diff", "--no-ext-diff", "HEAD")


def repository_key(repository: Path) -> str:
    common = git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return os.path.normcase(str(Path(common).resolve()))
