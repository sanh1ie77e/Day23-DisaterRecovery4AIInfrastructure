"""Additional behavioral checks, isolated from the live drill and its logs."""
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dr import failover as fo
from dr import health_checker as hc
from dr import runbook as rb
from state.seed_vectors import seed
from state import snapshot


def events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seed("a", 4, 1)
    seed("b", 0, 0)
    Path("edge").mkdir()
    Path("edge/active_region").write_text("a", encoding="utf-8")

    def state_of(region):
        base = Path(f"state/region-{region}")
        with sqlite3.connect(base / "vectors.sqlite") as con:
            count = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        return {"region": region, "count": count,
                "weights": (base / "weights/model.bin").exists(),
                "pool_state": (base / "pool_state").read_text()}

    monkeypatch.setattr(fo, "state_of", state_of)
    return tmp_path


def test_actual_readiness_timeout_does_not_cut_over(isolated_state, monkeypatch):
    snapshot.put("a", "fs")
    monkeypatch.setattr(fo.httpx, "get", lambda *args, **kwargs:
                        httpx.Response(503, json={"region": "b", "reasons": ["warming_up"]}))
    result = fo.failover("b", "fs", wait=0.05)
    assert not result["ok"] and not result["cutover"]
    assert Path("edge/active_region").read_text() == "a"
    log = events(fo.LOG)
    assert [event["step"] for event in log] == ["1_verify_target", "2_restore_snapshot",
                                                "3_scale_pool", "4_wait_ready"]
    assert not log[-1]["ok"]
    assert result["docs_lost"] == 0


def test_missing_snapshot_aborts_before_scale(isolated_state):
    result = fo.failover("b", "fs", wait=0.05)
    assert not result["ok"]
    assert Path("edge/active_region").read_text() == "a"
    assert Path("state/region-b/pool_state").read_text() == "warm"
    assert events(fo.LOG)[-1]["step"] == "2_restore_snapshot"


def test_ready_target_cutover_has_complete_order_and_metrics(isolated_state, monkeypatch):
    snapshot.put("a", "fs")
    monkeypatch.setattr(fo.httpx, "get", lambda *args, **kwargs:
                        httpx.Response(200, json={"region": "b", "ready": True, "reasons": []}))
    result = fo.failover("b", "fs", wait=1)
    assert result["ok"] and result["cutover"]
    assert Path("edge/active_region").read_bytes() == b"b"
    assert result["state"]["count"] == 4 and result["state"]["weights"]
    assert result["rpo_seconds"] == 0 and result["docs_lost"] == 0
    log = events(fo.LOG)
    assert [event["step"] for event in log] == ["1_verify_target", "2_restore_snapshot",
                                                "3_scale_pool", "4_wait_ready", "5_dns_cutover"]
    assert log[3]["ts"] <= log[4]["ts"]
    assert log[1]["embed_model_version"] == "embed-model=vi-e5-base@v3"


@pytest.mark.parametrize("answer,expected", [("y", True), ("Y", True), ("", False), ("n", False)])
def test_manual_confirmation_defaults_to_no(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda message: answer)
    assert rb.confirm(False, "failover?") is expected


def test_auto_confirmation_does_not_prompt(monkeypatch):
    def forbidden(message):
        raise AssertionError("auto mode must not prompt")
    monkeypatch.setattr("builtins.input", forbidden)
    assert rb.confirm(True, "failover?") is True


def test_probe_reports_timeout_and_readiness(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 0.2
        raise httpx.ReadTimeout("simulated partition")
    monkeypatch.setattr(hc.httpx, "get", timeout)
    assert hc.probe("a", 0.2) == (False, "timeout")
    monkeypatch.setattr(hc.httpx, "get", lambda *args, **kwargs: httpx.Response(200))
    assert hc.probe("a", 0.2) == (True, "ready")


def test_runbook_invokes_failover_once_and_records_seven_steps(isolated_state, monkeypatch):
    calls = []
    requests = []
    monkeypatch.setattr(rb.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rb.hc, "probe", lambda region, timeout: (False, "timeout"))
    monkeypatch.setattr(rb.httpx, "get", lambda *args, **kwargs:
                        httpx.Response(200, json={"region": "b", "alive": True}))

    def fake_failover(target, backend, wait):
        calls.append((target, backend, wait))
        Path("edge/active_region").write_text("b")
        return {"ok": True, "cutover": True, "state": {"count": 4, "weights": True},
                "rpo_seconds": 2.0, "docs_lost": 1, "embed_model_version": "test"}

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            requests.append(url)
            return httpx.Response(200, json={"region": "b"})

    monkeypatch.setattr(rb.fo, "failover", fake_failover)
    monkeypatch.setattr(rb.httpx, "Client", Client)
    result = rb.run("a", "b", "fs", auto=True)
    assert result["ok"] and len(calls) == 1 and len(requests) == 10
    log = events(rb.LOG)
    assert [event["step"] for event in log] == list(range(1, 8))
    assert log[5]["error_rate"] == 0


def test_healthy_primary_prevents_failover(isolated_state, monkeypatch):
    monkeypatch.setattr(rb.hc, "probe", lambda region, timeout: (True, "ready"))
    monkeypatch.setattr(rb.httpx, "get", lambda *args, **kwargs:
                        httpx.Response(200, json={"region": "b", "alive": True}))
    def forbidden(*args, **kwargs):
        raise AssertionError("healthy primary must not trigger failover")
    monkeypatch.setattr(rb.fo, "failover", forbidden)
    assert not rb.run("a", "b", "fs", auto=True)["ok"]
