"""The tray icon's helpers (the Windows parts only run on Windows; CI starts camtray.exe there)."""
import json

from camhub import tray


def test_read_port(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server": {"port": 8443}}))
    assert tray.read_port(str(cfg)) == 8443


def test_read_port_falls_back(tmp_path):
    assert tray.read_port(str(tmp_path / "missing.json")) == 8080
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert tray.read_port(str(bad)) == 8080
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"server": {"port": 0}}))
    assert tray.read_port(str(odd)) == 8080


def test_viewer_command_uses_edge_app_window():
    assert tray.viewer_command(8080, edge="msedge.exe") == ["msedge.exe", "--app=http://localhost:8080/"]
    assert tray.viewer_command(8080, edge="") is None


def test_edge_path(tmp_path):
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_text("")
    assert tray.edge_path({"ProgramFiles(x86)": str(tmp_path)}) == str(edge)
    assert tray.edge_path({}) is None


def test_tooltip_and_status():
    assert "recording" in tray.tooltip(True)
    assert len(tray.tooltip(False)) < 128
    assert tray.status_text(False) == "Recorder not running"


def test_recorder_running_false_when_nothing_listens():
    assert tray.recorder_running(1, timeout=0.5) is False
