# D23 — Lê Văn Sang — MSSV 2A202602391

Bài nộp: **D23-LeVanSang-2A202602391**. Thực hiện ngày 09/10/2026, Ubuntu/WSL 2, bare mode, backend fs.

- Baseline: NO_RECOVERY, 16 request lỗi.
- Drill có DR: valid=true, warnings=[], RTO **28.9s / 300s (PASS)**.
- RPO tại restore: **6.0s / 3 documents**.
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
