"""Build a self-contained submission with code, reports and raw evidence."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "D23-LeVanSang-2A202602391"


def main():
    destination = ROOT / NAME
    if destination.exists():
        raise RuntimeError("Existing submission must be preserved")
    destination.mkdir()
    source_files = ["README.md", "GUIDE.md", "RUBRIC.md", "requirements.txt", "Makefile"]
    for folder in ("dr", "serving", "edge", "state", "chaos", "loadgen", "tools", "tests"):
        source_files.extend(str(path.relative_to(ROOT)) for path in (ROOT / folder).glob("*.py"))
    source_files.extend(["scripts/up_bare.sh", "scripts/down_bare.sh", "chaos/chaos-events.jsonl"])
    source_files.extend(f"reports/{name}" for name in [
        "rto-evidence.md", "runbook.md", "postmortem.md", "drill-1-nodr.jsonl",
        "drill-2-withdr.jsonl", "health-events.jsonl", "failover-events.jsonl",
        "runbook-run.jsonl", "replication.jsonl", "measure-drill-1.json",
        "measure-drill-2.json", "setup-evidence.json", "final-state.json",
        "environment.txt", "environment-lock.txt", "pytest-results.txt"])
    source_files.extend(["run/execute_drills.py", "run/write_reports.py", "run/test_dr_safety.py"])
    for relative in source_files:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    measured = json.loads((ROOT / "reports/measure-drill-2.json").read_text())
    intro = f"""# D23 — Lê Văn Sang — MSSV 2A202602391

Bài nộp: **{NAME}**. Thực hiện ngày 09/10/2026, Ubuntu/WSL 2, bare mode, backend fs.

- Baseline: NO_RECOVERY, 16 request lỗi.
- Drill có DR: valid=true, warnings=[], RTO **{measured['rto_measured_s']}s / 300s (PASS)**.
- RPO tại restore: **{measured['rpo_at_restore_s']}s / {measured['docs_lost']} documents**.
- Kết quả kiểm tra: xem reports/pytest-results.txt; có tests gốc và tests safety bổ sung.

Sáu file bắt buộc: dr/health_checker.py, dr/failover.py, dr/runbook.py và reports/rto-evidence.md, reports/runbook.md, reports/postmortem.md. Raw logs trong reports/ và chaos/ là evidence bắt buộc để kiểm tra path:line. Các source hỗ trợ, tests, scripts và requirements gốc được kèm để chấm trực tiếp; không kèm virtual environment, weights hay state runtime.

## Chấm bài trên Ubuntu/WSL

Tại thư mục chứa README này, dùng Python 3.10+ và môi trường có dependencies của requirements.txt. Boto3 chỉ cần khi chọn MinIO; bài này dùng fs. Chạy:

```bash
python -m pytest tests/ -v
python -m pytest run/test_dr_safety.py -v
python tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
```

Không cần bật services để chấm unit/evidence tests. Test failover có thể append các event mô phỏng sau cửa sổ drill; dụng cụ đo lọc đúng cửa sổ timestamps để không nhặt nhầm các event này.

## Tái lập drill

Chạy theo GUIDE.md trong một checkout/thư mục mới, để giữ nguyên evidence của bài nộp này. Sau khi cài môi trường Linux, `python run/execute_drills.py` điều phối đúng các lệnh seed/start/traffic/chaos/restore/replicate/runbook và dừng services ở cuối. Script cố ý từ chối ghi đè hai file traffic đã tồn tại. Không dùng Python Windows cho SIGSTOP/SIGCONT. Đảm bảo scripts/*.sh dùng LF.

run/write_reports.py đọc logs thật để tính số và line evidence. Dụng cụ đo/tests/serving/chaos gốc được giữ nguyên. SHA256SUMS.json dùng kiểm tra toàn vẹn nội dung bài nộp. Không sửa timestamps để đạt PASS.
"""
    (destination / "SUBMISSION.md").write_text(intro, encoding="utf-8")
    checksums = {path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(destination.rglob("*")) if path.is_file()}
    (destination / "SHA256SUMS.json").write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
    archive = ROOT / f"{NAME}.zip"
    if archive.exists():
        raise RuntimeError("Existing archive must be preserved")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(ROOT))
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
    print(json.dumps({"folder": str(destination), "zip": str(archive),
                      "files": len(checksums) + 1, "bytes": archive.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
