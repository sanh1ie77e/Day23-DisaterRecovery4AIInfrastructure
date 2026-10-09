# Runbook — Region A down, phục hồi sang B

**Owner:** Lê Văn Sang — MSSV 2A202602391 (on-call); incident commander quyết định rollback.
**Phạm vi:** lab local, Ubuntu/WSL, bare mode, snapshot filesystem. Thực hiện từ thư mục gốc repo với môi trường `.venv` đã cài `requirements.txt` (`export PATH="$PWD/.venv/bin:$PATH"`). Mục tiêu RTO/RPO: 300 giây. Chỉ gây outage một region.

**Trước incident:** `bash scripts/up_bare.sh`; chạy replication ở terminal riêng: `python state/replicate.py --every 30 --duration 150 --backend fs`. Phải thấy `REPLICATE` và `state/_replica/dr-artifacts/MANIFEST.json` tồn tại. Khởi động checker: `python dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl`. Traffic đo RTO phải đang chạy: `python loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl`.

Lệnh ở bước 3 thực hiện toàn bộ 7 bước automation **một lần**. Các lệnh kiểm tra ở bước 4–7 chỉ đọc trạng thái/kết quả, không chạy lại failover.

| # | Bước | Lệnh copy-paste | Biết xong khi | Owner | Điều kiện dừng/rollback |
|---|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | A không ready ở 3 probe liên tiếp; B `alive=true`. Checker ghi `region=a,to=UNHEALTHY`. | on-call | A phục hồi hoặc B không alive: dừng, không chuyển traffic. |
| 2 | Mở incident + xác nhận | `date -Is` | Ghi nhận giờ vận hành; trả lời `y` tại prompt bước 3. Automation ghi `thong_bao_incident` cùng `t_outage` thật vào log. | on-call | Không đồng ý hoặc chưa xác nhận outage: trả lời N. |
| 3 | Restore + scale GPU pool | `python dr/runbook.py --primary a --target b --backend fs` | Năm sub-step `1_verify_target` → `2_restore_snapshot` → `3_scale_pool` → `4_wait_ready` → `5_dns_cutover`; kết quả `ok=true`. | on-call | Không có snapshot, không đo được RPO hoặc B chưa ready sau 60s: ABORT, giữ pointer cũ. |
| 4 | Verify state replica | `curl -fsS http://127.0.0.1:8002/v1/state` | B có `weights=true`, `count>0`, `pool_state=full`; log step 4 có RPO và embedding version. | on-call | Sai/mất state: báo commander, không gọi failover lần hai. |
| 5 | Verify DNS/LB cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | Sau tối đa TTL 5s: `active_region=b`; bước 5 runbook `ok=true`. | on-call | B mất readiness sau cutover: đánh giá rollback theo điều kiện bên dưới. |
| 6 | Verify golden signals | `python -c "import json,pathlib; print([json.loads(x) for x in pathlib.Path('reports/runbook-run.jsonl').read_text().splitlines() if json.loads(x).get('step') == 6][-1])"` | 10 inference thật tới B; error rate 0; p95 < 1000ms. Traffic Edge có `ok=true,served_by=b`. | on-call | Có lỗi hoặc p95 vượt ngưỡng: giữ incident mở, commander đánh giá rollback. |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `warnings=[]`, `rto_verdict=PASS`, RPO và docs_lost có số; điền evidence và postmortem. | Lê Văn Sang | INVALID/FAIL: giữ logs, xử lý nguyên nhân rồi chạy drill mới; không sửa timestamps. |

**Rollback:** không tự chuyển qua lại. Commander chỉ duyệt trả traffic về A khi B không đạt golden signals/readiness, A đã `/readyz=200`, và đã đối chiếu/đồng bộ dữ liệu cùng embedding version để tránh bỏ mất writes sau cutover. Khôi phục process A bị SIGSTOP bằng `python chaos/kill_region.py restore --region a --backend bare`, rồi kiểm tra `curl -fsS http://127.0.0.1:8001/readyz` và `/v1/state` của cả hai region. Chỉ sau khi đủ điều kiện và commander duyệt, on-call chạy:

```bash
python -c "from pathlib import Path; from dr.failover import write_pointer,emit; write_pointer(Path('edge/active_region'),'a'); emit(step='rollback_dns_cutover',target='a',ok=True)"
```

Chờ TTL 5s rồi kiểm tra Edge inference. Nếu A chưa ready hoặc dữ liệu chưa đối soát, không rollback; ưu tiên sửa B và giữ incident mở. `--auto` chỉ dành cho drill/CI. Kết thúc lab bằng `bash scripts/down_bare.sh` sau khi đã lưu evidence.
