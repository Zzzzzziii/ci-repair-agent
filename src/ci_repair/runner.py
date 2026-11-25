from contextlib import suppress
import os
from pathlib import Path
import signal
import subprocess

from ci_repair.schemas import ProcessResult


OUTPUT_LIMIT = 12000
CLEANUP_TIMEOUT = 5


def log_tail(path: Path) -> str:
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - OUTPUT_LIMIT * 4))
        return stream.read().decode("utf-8", errors="replace")[-OUTPUT_LIMIT:]


def terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            raise RuntimeError("Parent exited before its process tree could be cleaned up")
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        cleanup = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=CLEANUP_TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if cleanup.returncode:
            raise RuntimeError(cleanup.stderr.strip() or f"taskkill exited with code {cleanup.returncode}")
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def stop_process(process: subprocess.Popen) -> str | None:
    try:
        terminate_process_tree(process)
        process.wait(timeout=CLEANUP_TIMEOUT)
        return None
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        details = str(error)

    try:
        process.kill()
        process.wait(timeout=CLEANUP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        details += f". Parent cleanup: {error}"
    return details


def run_process(
    command: list[str], *, cwd: Path, env: dict[str, str] | None,
    stdout_path: Path, stderr_path: Path, timeout: float,
) -> ProcessResult:
    timed_out = False
    exit_code = None
    cleanup_error = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
            start_new_session=os.name != "nt",
        )
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_error = stop_process(process)
        except KeyboardInterrupt:
            stop_process(process)
            raise

    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output": (log_tail(stdout_path) + log_tail(stderr_path))[-OUTPUT_LIMIT:],
        "cleanup_error": cleanup_error,
    }
