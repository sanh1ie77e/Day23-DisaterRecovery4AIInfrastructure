"""Validate the actual ZIP in an isolated extraction; leave deliverables intact."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "D23-LeVanSang-2A202602391"
extraction = ROOT / "run" / "submission-verification"
assert extraction.resolve().is_relative_to(ROOT)
if extraction.exists():
    raise RuntimeError("Preserve existing extraction")
extraction.mkdir()
with zipfile.ZipFile(ROOT / f"{NAME}.zip") as archive:
    for member in archive.infolist():
        assert (extraction / member.filename).resolve().is_relative_to(extraction.resolve())
    assert archive.testzip() is None
    archive.extractall(extraction)
package = extraction / NAME
checksums = json.loads((package / "SHA256SUMS.json").read_text())
for relative, expected in checksums.items():
    assert hashlib.sha256((package / relative).read_bytes()).hexdigest() == expected, relative
required = ["dr/health_checker.py", "dr/failover.py", "dr/runbook.py",
            "reports/rto-evidence.md", "reports/runbook.md", "reports/postmortem.md"]
assert all((package / relative).is_file() for relative in required)
result = subprocess.run([sys.executable, "-m", "pytest", "tests/", "run/test_dr_safety.py", "-v"],
                        cwd=package, capture_output=True, text=True)
print(result.stdout)
if result.returncode:
    print(result.stderr)
    raise SystemExit(result.returncode)
for port in (8001, 8002, 8080):
    with socket.socket() as sock:
        sock.settimeout(2)
        assert sock.connect_ex(("127.0.0.1", port)) != 0, f"lab port {port} still open"
message = {"zip_verified": True, "sha256_files_verified": len(checksums),
           "required_files_present": len(required), "tests": "24 passed in extracted submission",
           "lab_services_stopped": True}
print(json.dumps(message))
(ROOT / "reports/submission-verification.txt").write_text(
    result.stdout + "\n" + json.dumps(message, indent=2) + "\n", encoding="utf-8")
