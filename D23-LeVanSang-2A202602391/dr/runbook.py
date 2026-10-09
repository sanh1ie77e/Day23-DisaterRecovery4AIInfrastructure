"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step."""
    timestamp = time.time()
    record = {"ts": timestamp, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)),
              "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Ask for explicit confirmation unless running an automated drill."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def records(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # An independent writer can still be appending its last line.
            continue
    return result


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Confirm the incident and execute the seven-step response once."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be distinct regions a/b")
    started = time.monotonic()
    result = {"ok": False, "primary": primary, "target": target}

    def abort(reason):
        result["error"] = reason
        step(7, "post_incident", ok=False, reason=reason,
             elapsed_s=time.monotonic() - started)
        return result

    kills = [event for event in records(pathlib.Path("chaos/chaos-events.jsonl"))
             if event.get("action") == "kill" and event.get("region") == primary]
    outage_ts = kills[-1]["ts"] if kills else None
    probes = []
    for attempt in range(3):
        ready, reason = hc.probe(primary, 2.0)
        try:
            response = httpx.get(f"{URL[target]}/healthz", timeout=2.0)
            alive = response.status_code == 200 and response.json().get("region") == target
        except (httpx.HTTPError, ValueError):
            alive = False
        probes.append({"ts": time.time(), "primary_ready": ready, "reason": reason,
                       "target_alive": alive})
        if ready or not alive:
            step(1, "xac_nhan_outage", ok=False, probes=probes, t_outage=outage_ts)
            return abort("outage not confirmed or standby process unavailable")
        if attempt < 2:
            time.sleep(5.0)

    # When a checker is running for this drill, wait for its actual transition.
    # This prevents operator automation from overtaking the independent detector.
    health_path = pathlib.Path("reports/health-events.jsonl")
    health = records(health_path)
    detect_ts = None
    if outage_ts is not None and health and health[-1].get("ts", 0) >= outage_ts - 60:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            detection = next((event for event in records(health_path)
                              if event.get("region") == primary
                              and event.get("event") == "state_change"
                              and event.get("to") == "UNHEALTHY"
                              and event.get("ts", 0) >= outage_ts), None)
            if detection:
                detect_ts = detection["ts"]
                break
            time.sleep(0.25)
        if detect_ts is None:
            step(1, "xac_nhan_outage", ok=False, probes=probes, t_outage=outage_ts)
            return abort("health checker did not confirm this outage within 60s")
    step(1, "xac_nhan_outage", ok=True, probes=probes, t_outage=outage_ts,
         t_detect=detect_ts, consecutive_fails=3)
    if not confirm(auto, f"Confirm failover {primary} -> {target}?"):
        return abort("operator declined failover")
    incident = step(2, "thong_bao_incident", primary=primary, target=target,
                    t_outage=outage_ts, auto=auto)
    result["incident_ts"] = incident["ts"]

    failover_result = fo.failover(target, backend, wait=60.0)
    result["failover"] = failover_result
    step(3, "scale_gpu_pool", ok=failover_result["ok"], result=failover_result)
    if not failover_result["ok"]:
        return abort(failover_result.get("error", "failover failed"))
    state = failover_result["state"]
    replica_ok = state.get("weights") is True and state.get("count", 0) > 0
    step(4, "verify_state_replica", ok=replica_ok, state=state,
         rpo_seconds=failover_result["rpo_seconds"], docs_lost=failover_result["docs_lost"],
         embed_model_version=failover_result["embed_model_version"])
    cutover_ok = (failover_result["cutover"]
                  and pathlib.Path("edge/active_region").read_text(encoding="utf-8").strip() == target)
    step(5, "dns_cutover", ok=cutover_ok, active_region=target)
    if not replica_ok or not cutover_ok:
        return abort("replica/cutover verification failed; operator review required")

    samples = []
    errors = 0
    with httpx.Client(timeout=2.0) as client:
        for _ in range(10):
            request_started = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer")
                ok = response.status_code == 200 and response.json().get("region") == target
            except (httpx.HTTPError, ValueError):
                ok = False
            samples.append(round((time.monotonic() - request_started) * 1000, 2))
            errors += not ok
    p95 = sorted(samples)[9]  # nearest-rank p95 of 10 requests
    golden_ok = errors == 0 and p95 < 1000.0
    step(6, "verify_golden_signals", ok=golden_ok, requests=10, errors=errors,
         error_rate=errors / 10, p95_latency_ms=p95, p95_limit_ms=1000,
         latency_samples_ms=samples)
    result.update(ok=golden_ok, p95_latency_ms=p95, error_rate=errors / 10,
                  rpo_seconds=failover_result["rpo_seconds"], docs_lost=failover_result["docs_lost"])
    step(7, "post_incident", ok=result["ok"], elapsed_s=time.monotonic() - started,
         rpo_seconds=result["rpo_seconds"], docs_lost=result["docs_lost"],
         measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
