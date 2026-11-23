from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from uuid import uuid4


@contextmanager
def pytest_run_directory(scratch: Path, task_directory: Path | None) -> Iterator[Path]:
    if task_directory is None:
        with TemporaryDirectory(dir=scratch) as temporary:
            yield Path(temporary)
        return

    destination = task_directory / "artifacts" / "pytest" / uuid4().hex
    destination.mkdir(parents=True)
    yield destination
