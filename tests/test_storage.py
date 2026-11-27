"""Atomic UTF-8 file persistence and symlink-safe artifact snapshot tests."""

import json
from pathlib import Path

import pytest
from multi_round.storage import atomic_write_text, snapshot, write_json


def test_write_json_replaces_only_after_complete_utf8_write(tmp_path, monkeypatch):
    path = tmp_path / "nested/report.json"
    write_json(path, {"stage": "old — café"})
    original_replace = Path.replace

    def replace(source, target):
        assert json.loads(path.read_text(encoding="utf-8")) == {"stage": "old — café"}
        assert json.loads(source.read_text(encoding="utf-8")) == {"stage": "new — café"}
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    write_json(path, {"stage": "new — café"})

    raw = path.read_bytes()
    assert json.loads(raw.decode("utf-8")) == {"stage": "new — café"}
    # Verify ensure_ascii=False writes raw UTF-8 bytes rather than \uXXXX escapes.
    assert "new — café".encode("utf-8") in raw
    assert raw.endswith(b"\n")
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_atomic_write_failure_preserves_previous_report(tmp_path, monkeypatch, failure):
    path = tmp_path / "report.md"
    path.write_text("original report", encoding="utf-8")
    original_write = Path.write_text

    def fail_write(staged, text, **kwargs):
        original_write(staged, text[:2], **kwargs)
        raise OSError("write interrupted")

    def fail_replace(staged, target):
        raise OSError("replace failed")

    if failure == "write":
        monkeypatch.setattr(Path, "write_text", fail_write)
    else:
        monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="write interrupted|replace failed"):
        atomic_write_text(path, "updated report")

    assert path.read_text(encoding="utf-8") == "original report"
    assert list(tmp_path.iterdir()) == [path]


def test_declared_artifacts_exclude_harness_dependencies(tmp_path):
    source = tmp_path / "workspace"
    (source / "output").mkdir(parents=True)
    (source / "output/result.txt").write_text("result", encoding="utf-8")
    (source / "node_modules").symlink_to(tmp_path)
    target = tmp_path / "submission"
    hashes = snapshot(source, target, ["output", "materials"])
    assert set(hashes) == {"output/result.txt"}
    assert not (target / "node_modules").exists()


def test_link_inside_declared_artifacts_rejected(tmp_path):
    source = tmp_path / "workspace"
    (source / "output").mkdir(parents=True)
    (source / "output/external").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        snapshot(source, tmp_path / "submission", ["output"])


def test_artifact_path_cannot_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes root"):
        snapshot(tmp_path, tmp_path / "submission", ["../outside"])
