"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sqlite3
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append a timestamped JSONL event and print it for the operator."""
    timestamp = time.time()
    record = {"ts": timestamp, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    result = response.json()
    if result.get("region") != region:
        raise ValueError("target endpoint reports a different region")
    return result


def write_pointer(path: pathlib.Path, value: str):
    """Replace the pointer atomically; never expose a partial/BOM-containing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, scale, wait for readiness, and only then switch the edge."""
    if target not in URL or backend not in ("fs", "minio") or wait <= 0:
        raise ValueError("invalid target/backend/wait")
    primary = "b" if target == "a" else "a"
    result = {"ok": False, "target": target, "steps": [], "rpo_seconds": None,
              "docs_lost": None, "cutover": False}
    current_step = "1_verify_target"
    try:
        before = state_of(target)
        emit(step=current_step, target=target, ok=True, state=before)
        result["steps"].append(current_step)

        current_step = "2_restore_snapshot"
        restore_started = time.monotonic()
        metadata = snapshot.get(target, backend)
        metrics = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                               pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        if metrics["rpo_seconds"] is None or metrics["docs_lost"] is None:
            raise ValueError("snapshot restored, but RPO cannot be measured")
        result.update(metrics)
        result["embed_model_version"] = metadata["embed_model_version"]
        emit(step=current_step, target=target, ok=True, **metrics,
             embed_model_version=metadata["embed_model_version"],
             snapshot_at=metadata["snapshot_at"], duration_s=time.monotonic() - restore_started)
        result["steps"].append(current_step)

        current_step = "3_scale_pool"
        write_pointer(pathlib.Path(f"state/region-{target}/pool_state"), "full")
        emit(step=current_step, target=target, ok=True, pool_state="full")
        result["steps"].append(current_step)

        current_step = "4_wait_ready"
        started = time.monotonic()
        deadline = started + wait
        reason = "readiness timeout"
        ready = False
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2.0, max(0.001, deadline - time.monotonic())))
                body = response.json()
                ready = response.status_code == 200 and body.get("region") == target
                reason = body.get("reasons", f"HTTP {response.status_code}")
                if ready:
                    break
            except (httpx.HTTPError, ValueError) as exc:
                reason = str(exc)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.25, remaining))
        emit(step=current_step, target=target, ok=ready, waited_s=time.monotonic() - started,
             reason=reason)
        if not ready:
            result["error"] = "target not ready before deadline; cutover aborted"
            return result
        result["steps"].append(current_step)
        result["state"] = state_of(target)

        current_step = "5_dns_cutover"
        write_pointer(pathlib.Path("edge/active_region"), target)
        emit(step=current_step, target=target, ok=True, active_region=target)
        result["steps"].append(current_step)
        result.update(ok=True, cutover=True)
        return result
    except (httpx.HTTPError, OSError, ValueError, KeyError, sqlite3.Error, SystemExit) as exc:
        emit(step=current_step, target=target, ok=False, reason=str(exc))
        result["error"] = str(exc)
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
