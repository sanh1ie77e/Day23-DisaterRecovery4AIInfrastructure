# RTO/RPO Evidence — Lê Văn Sang — 2A202602391

Ngày drill: 2026-10-09 (Asia/Saigon, UTC+07:00). Chạy Ubuntu/WSL 2, Python 3.14.4, bare `--mock`, backend `fs`; warm-up 6 giây, Edge TTL 5 giây. Timestamps là logs thực, không dùng số minh họa trên slide. Chi tiết dependency ở `reports/environment.txt`.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị đo được | Evidence |
|---|---|---|
| Outage A | 2026-10-09T10:29:35.107+07:00 | `chaos/chaos-events.jsonl:1` |
| Request fail đầu | +0.0565s; latency 2032.7ms | `reports/drill-1-nodr.jsonl:17` |
| Tổng requests / failed | 32 / 16 | `reports/measure-drill-1.json:1`; `reports/drill-1-nodr.jsonl:32` |
| Request phục hồi trong cửa sổ baseline | Không có | `reports/measure-drill-1.json:1` |
| RTO verdict | NO_RECOVERY | `reports/measure-drill-1.json:1` |

Traffic chạy 40 giây; gây outage sau khoảng 8 giây. Không bật checker/runbook ở baseline. Chỉ khôi phục A sau khi traffic kết thúc, nên không có phục hồi trong phép đo baseline.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Signal | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0.0000s | action=kill, region=a | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.0277s | ok=false | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | 19.1843s | region=a, to=UNHEALTHY | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 19.5891s | 2_restore_snapshot | `reports/failover-events.jsonl:8` |
| Region B ready | 25.6729s | 4_wait_ready, ok=true | `reports/failover-events.jsonl:10` |
| DNS cutover | 25.7422s | 5_dns_cutover | `reports/failover-events.jsonl:11` |
| RTO / request thành công đầu từ B | 28.8943s | ok=true, served_by=b | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO Inference API | **28.9s** | 300s | PASS; dư 271.1s | `reports/measure-drill-2.json:1` |
| RPO Vector DB | **6.00s / 3 docs** | 300s | PASS; dư 294.00s | `reports/failover-events.jsonl:8` |

Drill hợp lệ: `valid=true`, `warnings=[]`, region phục hồi B khác region bị kill A. `other_alive=true`, `forced_both=false` ở sự kiện chaos. B được restore cả vectors, weights và embedding version `embed-model=vi-e5-base@v3`. RPO do `snapshot.rpo()` so sánh timestamp document của primary với bản restore, không lấy tuổi snapshot thay thế.

## 3. RTO breakdown — tổng khớp timestamps

| Thành phần | Giây thực tế | Cách tính / Evidence | Cách giảm |
|---|---|---|---|
| Detection, gồm polling phase và probe timeout | 19.1843s | t_detect − t_outage; `reports/health-events.jsonl:2` | Giảm interval sau khi đánh giá false positives. |
| Snapshot restore + verify/điều phối tới scale | 0.4239s | t_scale − t_detect; `reports/failover-events.jsonl:9` | Pre-stage snapshot/weights; giảm I/O. |
| GPU pool warm-up + kiểm tra ready tới cutover | 6.1340s | t_cutover − t_scale; `reports/failover-events.jsonl:10` | Giữ pool full/ready trước outage, tăng chi phí. |
| DNS/LB TTL + nhịp request | 3.1521s | t_recovered − t_cutover; `reports/drill-2-withdr.jsonl:40` | Giảm TTL; kiểm tra ảnh hưởng cache. |
| Tổng chưa làm tròn | 28.8943s | Tổng bốn khoảng liền nhau; measure làm tròn thành 28.9s | Không cộng lặp các khoảng. |

**Health-check detect floor theo cấu hình lab:** 15.0s = 5s × 3 (51.9% RTO); evidence `reports/health-events.jsonl:2`. Detection quan sát thực tế 19.1843s (66.4% RTO), vì phase polling và request timeout. Checker nghỉ interval sau mỗi vòng probe, nên timeout cộng vào lịch poll. Restore thuần đo bằng `duration_s=0.186475`; `waited_s=6.059751` là thời gian chờ readiness thực tế.

## 4. Golden signals và giới hạn phép đo

Runbook đủ 7 bước, gọi failover một lần. Golden signals: 10 request thật tới B, 0 lỗi, error rate 0%, p95 65.16ms < 1000ms; evidence `reports/runbook-run.jsonl:6`. Traffic phục hồi qua Edge được chứng minh độc lập ở bảng mốc.

Theo dụng cụ có sẵn, `loadgen.ts` ghi thời điểm bắt đầu request; RTO là timestamp request OK đầu sau lỗi trừ timestamp chaos, không phải timestamp hoàn tất response. RPO/docs_lost là độ thiếu dữ liệu tại restore; ingest riêng vẫn tiếp tục ghi primary trong lúc API A bị SIGSTOP. Đây là mô phỏng local, không chứng minh độ bền dữ liệu multi-region thật. Kết thúc, A được SIGCONT và cả hai region ready trước khi dừng services; evidence `reports/final-state.json:1`.
