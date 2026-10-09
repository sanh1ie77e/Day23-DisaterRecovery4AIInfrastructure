# Blameless Postmortem — DR Drill Lab 23

Người thực hiện: **Lê Văn Sang — 2A202602391**. Ngày 2026-10-09, giờ Asia/Saigon (UTC+07:00). Phạm vi: A/B/Edge trên cùng máy qua Ubuntu/WSL; outage `netblock --mock` chỉ Region A.

## 1. Tóm tắt và timeline

Baseline có 16/32 requests thất bại và NO_RECOVERY. Sau triển khai DR, user bị ảnh hưởng trong khoảng RTO 28.9s; hệ thống phục hồi bằng B. Trigger là SIGSTOP của A, tạo timeout; nguyên nhân khiến baseline không tự phục hồi nằm ở thiết kế/automation chưa có, không quy lỗi người chạy drill.

| ISO time (UTC+07) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T10:30:27.699+07:00 | t_outage (mốc 0) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T10:30:27.727+07:00 | User thấy lỗi đầu tiên | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T10:30:46.883+07:00 | Health check phát hiện | `reports/health-events.jsonl:2` |
| 2026-10-09T10:30:47.007+07:00 | Operator confirm / mở incident | `reports/runbook-run.jsonl:2` |
| 2026-10-09T10:30:47.288+07:00 | Snapshot restore xong | `reports/failover-events.jsonl:8` |
| 2026-10-09T10:30:53.372+07:00 | Region B ready | `reports/failover-events.jsonl:10` |
| 2026-10-09T10:30:53.441+07:00 | DNS cutover | `reports/failover-events.jsonl:11` |
| 2026-10-09T10:30:56.593+07:00 | RTO / request thành công đầu từ B | `reports/drill-2-withdr.jsonl:40` |

Incident được ghi sau outage 19.3081s; kết thúc runbook ở 2026-10-09T10:30:53.924+07:00, evidence `reports/runbook-run.jsonl:7`. Golden signals trực tiếp B có p95 65.16ms, error rate 0%, evidence `reports/runbook-run.jsonl:6`. Mốc resolved cho user là request thành công qua Edge, không lấy thời điểm runbook kết thúc làm RTO.

## 2. RTO/RPO và gap analysis

| Chỉ số | Mục tiêu | Đo được | Gap (đo − mục tiêu) | Kết luận |
|---|---|---|---|---|
| RTO | 300s | 28.9s | -271.1s | PASS; dư 271.1s |
| RPO | 300s | 6.00s / 3 docs | -294.00s | PASS; 3 docs chưa có trong bản restore |

Evidence RTO: `reports/measure-drill-2.json:1`; RPO: `reports/failover-events.jsonl:8`. Detection chiếm 19.1843s (66.4% RTO), là phần lớn nhất trong lần đo này. Budget detect floor 5 × 3 = 15.0s; phase polling, ba probe timeout và lịch nghỉ sau vòng làm detection dài hơn budget. Warm-up tới cutover 6.1340s, restore/điều phối 0.4239s và TTL/nhịp request 3.1521s; tổng timestamps 28.8943s. Chi tiết evidence từng thành phần ở reports/rto-evidence.md.

## 3. Root cause — 5 whys

1. Vì sao user không nhận inference? Region A không trả lời request trong network-partition mô phỏng, Edge timeout; evidence `reports/drill-1-nodr.jsonl:17`.
2. Vì sao baseline không phục hồi? Chưa có checker với threshold và quy trình failover chạy khi primary mất readiness; kết quả NO_RECOVERY ở `reports/measure-drill-1.json:1`.
3. Vì sao không thể chuyển ngay sang B? B sống nhưng bắt đầu `count=0`, `weights=false`, pool warm; evidence `reports/setup-evidence.json:1`. Liveness không đủ để serve inference.
4. Vì sao standby không có state sẵn? Thiết kế ban đầu cố ý chưa có chu kỳ backup/restore vectors và weights/version. Backup phải được chạy trước outage; evidence chu kỳ thực tế `reports/replication.jsonl:1` và restore `reports/failover-events.jsonl:8`.
5. Vì sao gap này chưa được phát hiện trước drill? Trạng thái khởi đầu chưa có readiness gate, runbook xác nhận và evidence end-to-end. Giải pháp là kiểm tra khả năng phục hồi thường xuyên và chặn cutover trước ready; không dựa vào việc process đã chạy.

## 4. Action items — owner và deadline

| # | Action item | Owner | Deadline (Asia/Saigon) | Kết quả cần kiểm chứng |
|---|---|---|---|---|
| 1 | So sánh interval 5s với 1s qua 5 drill, giữ threshold 3, đo false positives và RTO. | Lê Văn Sang / on-call | 2026-10-10 | Budget detect giảm lý thuyết 12s; chỉ nhận mức giảm thực tế từ logs. |
| 2 | Thử replication mỗi 10s, kiểm tra DB snapshot nhất quán và version tương thích. | Lê Văn Sang / state owner | 2026-10-11 | Budget chu kỳ giảm 20s; báo cả RPO seconds và docs_lost từng run. |
| 3 | Thêm kiểm tra định kỳ backup tồn tại, standby readiness và drill failover; lưu raw evidence. | Lê Văn Sang / CI owner | 2026-10-12 | Không cutover khi target chưa ready; snapshot lỗi phải ABORT; tránh gap không được quan sát. |
| 4 | Đánh giá standby full-ready so với warm standby và bổ sung quy trình đối soát writes trước failback. | Incident commander + Lê Văn Sang | 2026-10-12 | Có thể giảm warm-up khoảng 6s, đánh đổi chi phí; không cam kết trước khi đo. |

## 5. Ba câu hỏi bắt buộc và reflection

1. Interval × threshold = 15.0s, bằng 51.9% RTO. Detection thực tế 19.1843s, bằng 66.4% RTO; không thay số đo bằng budget.
2. Hạ interval xuống 1s với threshold 3 làm budget từ 15s xuống 3s, giảm lý thuyết 12s. RTO thực tế còn phụ thuộc timeout, lịch poll, xác nhận và TTL nên cần chạy lại. Đánh đổi: probe thường hơn và dễ xác nhận outage do transient network kéo dài, cần đánh giá flapping/false positives.
3. Nếu A mất dữ liệu vĩnh viễn trong outage 6 giờ, 3 docs là lượng primary có nhưng B thiếu tại restore của drill này; không phải ước tính tổng mất dữ liệu của một outage 6 giờ thật. Với khách hàng, đó có thể là ticket/tri thức mới chưa truy xuất được; cần đối soát nguồn, replay ingest và thông báo phạm vi ảnh hưởng. Trong lab, DB A còn trên đĩa nên docs_lost không đồng nghĩa mất vĩnh viễn.

Checker là process riêng, không import serving; API A bị SIGSTOP không dừng checker. Muốn chứng minh mục tiêu 5 phút, dùng traffic JSONL cùng chaos/health/failover logs và phép đo `tools/measure_rto.py`. Có thể giảm warm-up bằng standby ready hoặc giảm TTL trước khi hạ threshold; mỗi phương án cần đo chi phí và tác động.

## 6. Điều đã làm tốt và giới hạn

Threshold chống flapping, xác nhận y/N mặc định, giữ thứ tự 5 bước và chặn readiness timeout. Runbook gọi failover một lần, đọc lại kết quả ở step 4/5, rồi kiểm tra 10 inference thật. Logs có timestamps thật, version model, RPO và docs_lost; không double outage. Backend fs và SQLite cùng máy chỉ mô phỏng multi-region; warm-up là thời gian giả lập, không phải GPU thật. Ingest chạy riêng vẫn ghi A trong lúc API paused; RPO được chốt tại restore. Các tests/serving/chaos/measurement có sẵn được giữ nguyên.
