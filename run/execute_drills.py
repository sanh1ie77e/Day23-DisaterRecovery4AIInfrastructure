"""Coordinate the exact GUIDE commands in Linux; preserve real timestamps."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
ENV = os.environ.copy()
ENV["PATH"] = str(Path(sys.executable).parent) + os.pathsep + ENV["PATH"]
ENV["PYTHONUNBUFFERED"] = "1"
CHILDREN = []
LOGS = []


def command(*args):
    return subprocess.run([sys.executable, *args], env=ENV, check=True)


def background(name, *args):
    log = (ROOT / "run" / f"{name}.log").open("w", encoding="utf-8")
    LOGS.append(log)
    process = subprocess.Popen([sys.executable, *args], stdout=log,
                               stderr=subprocess.STDOUT, env=ENV)
    CHILDREN.append(process)
    return process


def measure(name, traffic):
    process = subprocess.run([sys.executable, "tools/measure_rto.py", "--loadgen", traffic,
                              "--target-rto", "300"], capture_output=True, text=True, env=ENV)
    print(process.stdout, flush=True)
    if process.returncode:
        raise RuntimeError(process.stderr or process.stdout)
    result = json.loads(process.stdout)
    Path(f"reports/measure-{name}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def wait_for(process):
    if process.wait() != 0:
        raise RuntimeError(f"process {process.args} failed; see run logs")


def main():
    for report in ("reports/drill-1-nodr.jsonl", "reports/drill-2-withdr.jsonl"):
        if Path(report).exists():
            raise RuntimeError(f"Preserve existing evidence: {report} already exists")
    for port in (8001, 8002, 8080):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Port {port} already occupied; existing process preserved")
    Path("reports").mkdir(exist_ok=True)
    command("state/seed_vectors.py", "--region", "a", "--docs", "200")
    command("state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0")
    Path("edge/active_region").write_text("a", encoding="utf-8")
    try:
        subprocess.run(["bash", "scripts/up_bare.sh"], env=ENV, check=True)
        with httpx.Client(timeout=3) as client:
            setup = {path: client.get(f"http://127.0.0.1:{port}{path}").json()
                     for port, path in [(8001, "/healthz"), (8002, "/v1/state"),
                                        (8080, "/edge/state"), (8080, "/v1/infer")]}
            assert setup["/v1/state"]["region"] == "b"
            assert setup["/v1/state"]["count"] == 0
            assert setup["/v1/state"]["weights"] is False
            assert setup["/v1/infer"]["edge_region"] == "a"
        Path("reports/setup-evidence.json").write_text(
            json.dumps(setup, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("BASELINE: traffic 40s; outage after 8s", flush=True)
        baseline = background("traffic-baseline", "loadgen/traffic.py", "--duration", "40",
                              "--rps", "2", "--out", "reports/drill-1-nodr.jsonl")
        time.sleep(8)
        command("chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock")
        wait_for(baseline)
        first = measure("drill-1", "reports/drill-1-nodr.jsonl")
        assert first["rto_verdict"] == "NO_RECOVERY" and first["requests_failed"] > 0
        command("chaos/kill_region.py", "restore", "--region", "a", "--backend", "bare")
        assert httpx.get("http://127.0.0.1:8001/readyz", timeout=3).status_code == 200

        print("DR: ingest/replicate 150s, traffic/checker 100s, outage after 12s", flush=True)
        ingest = background("ingest", "state/ingest.py", "--region", "a", "--rate", "0.5",
                            "--duration", "150")
        replication = background("replicate", "state/replicate.py", "--every", "30",
                                 "--duration", "150", "--backend", "fs")
        time.sleep(5)
        manifest = Path("state/_replica/dr-artifacts/MANIFEST.json")
        assert manifest.exists(), "First snapshot must finish before the outage"
        traffic = background("traffic-dr", "loadgen/traffic.py", "--duration", "100",
                             "--rps", "2", "--out", "reports/drill-2-withdr.jsonl")
        checker = background("health-checker", "dr/health_checker.py", "--interval", "5",
                             "--threshold", "3", "--duration", "100",
                             "--out", "reports/health-events.jsonl")
        time.sleep(12)
        command("chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock")
        command("dr/runbook.py", "--primary", "a", "--target", "b", "--backend", "fs", "--auto")
        wait_for(traffic)
        wait_for(checker)
        second = measure("drill-2", "reports/drill-2-withdr.jsonl")
        assert second["valid"] and not second["warnings"]
        assert second["rto_verdict"] == "PASS" and second["recovered_by_region"] == "b"
        wait_for(ingest)
        wait_for(replication)
        command("chaos/kill_region.py", "restore", "--region", "a", "--backend", "bare")
        final = {region: httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=3).json()
                 for region, port in [("a", 8001), ("b", 8002)]}
        assert all(state["ready"] for state in final.values())
        Path("reports/final-state.json").write_text(json.dumps(final, indent=2) + "\n", encoding="utf-8")
    finally:
        for process in CHILDREN:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        subprocess.run(["bash", "scripts/down_bare.sh"], env=ENV, check=True)
        for log in LOGS:
            log.close()


if __name__ == "__main__":
    main()
