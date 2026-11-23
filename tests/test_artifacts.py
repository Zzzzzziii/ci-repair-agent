from pathlib import Path

import pytest

from ci_repair.artifacts import write_json


def test_write_json_keeps_previous_report_when_replace_fails(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    write_json(report, {"status": "paused"})
    original = report.read_bytes()

    def fail_replace(self, target):
        raise PermissionError("Cannot replace report")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError, match="Cannot replace report"):
        write_json(report, {"status": "accepted"})

    assert report.read_bytes() == original
    assert list(tmp_path.iterdir()) == [report]
