"""Benchmark-only CLI bootstrap. The model runs on the host and pytest runs in Docker."""
from functools import partial
import sys

from ci_repair import cli
from ci_repair.pytest_adapter import PytestAdapter
from isolated import reporter_runner


if __name__ == "__main__":
    adapter = partial(PytestAdapter, process_runner=reporter_runner(sys.argv[1]))
    raise SystemExit(cli.main(sys.argv[2:], adapter_factory=adapter))
