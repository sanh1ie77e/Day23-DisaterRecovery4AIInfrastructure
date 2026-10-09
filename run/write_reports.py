"""Derive report values and evidence line numbers from untouched drill logs."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAIGON = timezone(timedelta(hours=7))


def load(path):
    return [(number, json.loads(line)) for number, line in
            enumerate((ROOT / path).read_text(encoding="utf-8").splitlines(), 1) if line.strip()]


def iso(ts):
    return datetime.fromtimestamp(ts, SAIGON).isoformat(timespec="milliseconds")


def ref(path, item):
    return f"`{path}:{item[0]}`"


def main():
    measure1 = json.loads((ROOT / "reports/measure-drill-1.json").read_text())
    measure2 = json.loads((ROOT / "reports/measure-drill-2.json").read_text())
    assert measure1["rto_verdict"] == "NO_RECOVERY"
    assert measure2["valid"] and not measure2["warnings"] and measure2["rto_verdict"] == "PASS"
    traffic1 = load("reports/drill-1-nodr.jsonl")
    traffic2 = load("reports/drill-2-withdr.jsonl")
    chaos = load("chaos/chaos-events.jsonl")

    def kill_for(traffic):
        return next(item for item in reversed(chaos) if item[1].get("action") == "kill"
                    and traffic[0][1]["ts"] <= item[1]["ts"] <= traffic[-1][1]["ts"])

    kill1, kill2 = kill_for(traffic1), kill_for(traffic2)
    t0 = kill2[1]["ts"]
    fail1 = next(item for item in traffic1 if item[1]["ts"] >= kill1[1]["ts"] and not item[1]["ok"])
    fail2 = next(item for item in traffic2 if item[1]["ts"] >= t0 and not item[1]["ok"])
    recovery = next(item for item in traffic2 if item[1]["ts"] > fail2[1]["ts"] and item[1]["ok"])
    assert recovery[1]["served_by"] == "b"
    health = load("reports/health-events.jsonl")
    detect = next(item for item in health if item[1]["ts"] >= t0
                  and item[1].get("region") == "a" and item[1].get("to") == "UNHEALTHY")
    failover = [item for item in load("reports/failover-events.jsonl")
                if t0 <= item[1]["ts"] <= traffic2[-1][1]["ts"]]
    by_step = {item[1]["step"]: item for item in failover}
    assert list(by_step) == ["1_verify_target", "2_restore_snapshot", "3_scale_pool",
                             "4_wait_ready", "5_dns_cutover"]
    restore, scale, ready, cutover = [by_step[name] for name in
                                     ["2_restore_snapshot", "3_scale_pool", "4_wait_ready", "5_dns_cutover"]]
    runbook = [item for item in load("reports/runbook-run.jsonl") if item[1]["ts"] >= t0]
    assert [item[1]["step"] for item in runbook] == list(range(1, 8))
    incident, golden, summary = runbook[1], runbook[5], runbook[6]
    rto = measure2["rto_measured_s"]
    rpo = measure2["rpo_at_restore_s"]
    docs = measure2["docs_lost"]
    raw_rto = recovery[1]["ts"] - t0
    components = [detect[1]["ts"] - t0, scale[1]["ts"] - detect[1]["ts"],
                  cutover[1]["ts"] - scale[1]["ts"], recovery[1]["ts"] - cutover[1]["ts"]]
    assert abs(sum(components) - raw_rto) < 1e-6
    floor = detect[1]["interval_s"] * detect[1]["threshold"]
    pct = floor / raw_rto * 100
    actual_pct = components[0] / raw_rto * 100

    milestones = [
        ("t_outage (mốc 0)", kill2, "chaos/chaos-events.jsonl", "action=kill, region=a"),
        ("User thấy lỗi đầu tiên", fail2, "reports/drill-2-withdr.jsonl", "ok=false"),
        ("Health check phát hiện", detect, "reports/health-events.jsonl", "region=a, to=UNHEALTHY"),
        ("Snapshot restore xong", restore, "reports/failover-events.jsonl", "2_restore_snapshot"),
        ("Region B ready", ready, "reports/failover-events.jsonl", "4_wait_ready, ok=true"),
        ("DNS cutover", cutover, "reports/failover-events.jsonl", "5_dns_cutover"),
        ("RTO / request thành công đầu từ B", recovery, "reports/drill-2-withdr.jsonl", "ok=true, served_by=b"),
    ]
    rows = "\n".join(f"| {label} | {item[1]['ts'] - t0:.4f}s | {signal} | {ref(path, item)} |"
                     for label, item, path, signal in milestones)
    evidence = f"""# RTO/RPO Evidence — Lê Văn Sang — 2A202602391

Ngày drill: {iso(t0)[:10]} (Asia/Saigon, UTC+07:00). Chạy Ubuntu/WSL 2, Python 3.14.4, bare `--mock`, backend `fs`; warm-up 6 giây, Edge TTL 5 giây. Timestamps là logs thực, không dùng số minh họa trên slide. Chi tiết dependency ở `reports/environment.txt`.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị đo được | Evidence |
|---|---|---|
| Outage A | {iso(kill1[1]['ts'])} | {ref('chaos/chaos-events.jsonl', kill1)} |
| Request fail đầu | +{fail1[1]['ts'] - kill1[1]['ts']:.4f}s; latency {fail1[1]['latency_ms']:.1f}ms | {ref('reports/drill-1-nodr.jsonl', fail1)} |
| Tổng requests / failed | {len(traffic1)} / {measure1['requests_failed']} | `reports/measure-drill-1.json:1`; {ref('reports/drill-1-nodr.jsonl', traffic1[-1])} |
| Request phục hồi trong cửa sổ baseline | Không có | `reports/measure-drill-1.json:1` |
| RTO verdict | NO_RECOVERY | `reports/measure-drill-1.json:1` |

Traffic chạy 40 giây; gây outage sau khoảng 8 giây. Không bật checker/runbook ở baseline. Chỉ khôi phục A sau khi traffic kết thúc, nên không có phục hồi trong phép đo baseline.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Signal | Evidence |
|---|---|---|---|
{rows}

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO Inference API | **{rto:.1f}s** | 300s | PASS; dư {300-rto:.1f}s | `reports/measure-drill-2.json:1` |
| RPO Vector DB | **{rpo:.2f}s / {docs} docs** | 300s | PASS; dư {300-rpo:.2f}s | {ref('reports/failover-events.jsonl', restore)} |

Drill hợp lệ: `valid=true`, `warnings=[]`, region phục hồi B khác region bị kill A. `other_alive=true`, `forced_both=false` ở sự kiện chaos. B được restore cả vectors, weights và embedding version `{restore[1]['embed_model_version']}`. RPO do `snapshot.rpo()` so sánh timestamp document của primary với bản restore, không lấy tuổi snapshot thay thế.

## 3. RTO breakdown — tổng khớp timestamps

| Thành phần | Giây thực tế | Cách tính / Evidence | Cách giảm |
|---|---|---|---|
| Detection, gồm polling phase và probe timeout | {components[0]:.4f}s | t_detect − t_outage; {ref('reports/health-events.jsonl', detect)} | Giảm interval sau khi đánh giá false positives. |
| Snapshot restore + verify/điều phối tới scale | {components[1]:.4f}s | t_scale − t_detect; {ref('reports/failover-events.jsonl', scale)} | Pre-stage snapshot/weights; giảm I/O. |
| GPU pool warm-up + kiểm tra ready tới cutover | {components[2]:.4f}s | t_cutover − t_scale; {ref('reports/failover-events.jsonl', ready)} | Giữ pool full/ready trước outage, tăng chi phí. |
| DNS/LB TTL + nhịp request | {components[3]:.4f}s | t_recovered − t_cutover; {ref('reports/drill-2-withdr.jsonl', recovery)} | Giảm TTL; kiểm tra ảnh hưởng cache. |
| Tổng chưa làm tròn | {raw_rto:.4f}s | Tổng bốn khoảng liền nhau; measure làm tròn thành {rto:.1f}s | Không cộng lặp các khoảng. |

**Health-check detect floor theo cấu hình lab:** {floor:.1f}s = {detect[1]['interval_s']:g}s × {detect[1]['threshold']} ({pct:.1f}% RTO); evidence {ref('reports/health-events.jsonl', detect)}. Detection quan sát thực tế {components[0]:.4f}s ({actual_pct:.1f}% RTO), vì phase polling và request timeout. Checker nghỉ interval sau mỗi vòng probe, nên timeout cộng vào lịch poll. Restore thuần đo bằng `duration_s={restore[1]['duration_s']:.6f}`; `waited_s={ready[1]['waited_s']:.6f}` là thời gian chờ readiness thực tế.

## 4. Golden signals và giới hạn phép đo

Runbook đủ 7 bước, gọi failover một lần. Golden signals: {golden[1]['requests']} request thật tới B, {golden[1]['errors']} lỗi, error rate {golden[1]['error_rate']:.0%}, p95 {golden[1]['p95_latency_ms']:.2f}ms < 1000ms; evidence {ref('reports/runbook-run.jsonl', golden)}. Traffic phục hồi qua Edge được chứng minh độc lập ở bảng mốc.

Theo dụng cụ có sẵn, `loadgen.ts` ghi thời điểm bắt đầu request; RTO là timestamp request OK đầu sau lỗi trừ timestamp chaos, không phải timestamp hoàn tất response. RPO/docs_lost là độ thiếu dữ liệu tại restore; ingest riêng vẫn tiếp tục ghi primary trong lúc API A bị SIGSTOP. Đây là mô phỏng local, không chứng minh độ bền dữ liệu multi-region thật. Kết thúc, A được SIGCONT và cả hai region ready trước khi dừng services; evidence `reports/final-state.json:1`.
"""
    (ROOT / "reports/rto-evidence.md").write_text(evidence, encoding="utf-8")

    timeline_items = [(label, item, path) for label, item, path, _ in milestones]
    timeline_items.append(("Operator confirm / mở incident", incident, "reports/runbook-run.jsonl"))
    timeline = "\n".join(f"| {iso(item[1]['ts'])} | {label} | {ref(path, item)} |"
                         for label, item, path in sorted(timeline_items, key=lambda row: row[1][1]["ts"]))
    postmortem = f"""# Blameless Postmortem — DR Drill Lab 23

Người thực hiện: **Lê Văn Sang — 2A202602391**. Ngày {iso(t0)[:10]}, giờ Asia/Saigon (UTC+07:00). Phạm vi: A/B/Edge trên cùng máy qua Ubuntu/WSL; outage `netblock --mock` chỉ Region A.

## 1. Tóm tắt và timeline

Baseline có {measure1['requests_failed']}/{len(traffic1)} requests thất bại và NO_RECOVERY. Sau triển khai DR, user bị ảnh hưởng trong khoảng RTO {rto:.1f}s; hệ thống phục hồi bằng B. Trigger là SIGSTOP của A, tạo timeout; nguyên nhân khiến baseline không tự phục hồi nằm ở thiết kế/automation chưa có, không quy lỗi người chạy drill.

| ISO time (UTC+07) | Sự kiện | Evidence |
|---|---|---|
{timeline}

Incident được ghi sau outage {incident[1]['ts']-t0:.4f}s; kết thúc runbook ở {iso(summary[1]['ts'])}, evidence {ref('reports/runbook-run.jsonl', summary)}. Golden signals trực tiếp B có p95 {golden[1]['p95_latency_ms']:.2f}ms, error rate {golden[1]['error_rate']:.0%}, evidence {ref('reports/runbook-run.jsonl', golden)}. Mốc resolved cho user là request thành công qua Edge, không lấy thời điểm runbook kết thúc làm RTO.

## 2. RTO/RPO và gap analysis

| Chỉ số | Mục tiêu | Đo được | Gap (đo − mục tiêu) | Kết luận |
|---|---|---|---|---|
| RTO | 300s | {rto:.1f}s | {rto-300:.1f}s | PASS; dư {300-rto:.1f}s |
| RPO | 300s | {rpo:.2f}s / {docs} docs | {rpo-300:.2f}s | PASS; {docs} docs chưa có trong bản restore |

Evidence RTO: `reports/measure-drill-2.json:1`; RPO: {ref('reports/failover-events.jsonl', restore)}. Detection chiếm {components[0]:.4f}s ({actual_pct:.1f}% RTO), là phần lớn nhất trong lần đo này. Budget detect floor 5 × 3 = {floor:.1f}s; phase polling, ba probe timeout và lịch nghỉ sau vòng làm detection dài hơn budget. Warm-up tới cutover {components[2]:.4f}s, restore/điều phối {components[1]:.4f}s và TTL/nhịp request {components[3]:.4f}s; tổng timestamps {raw_rto:.4f}s. Chi tiết evidence từng thành phần ở reports/rto-evidence.md.

## 3. Root cause — 5 whys

1. Vì sao user không nhận inference? Region A không trả lời request trong network-partition mô phỏng, Edge timeout; evidence {ref('reports/drill-1-nodr.jsonl', fail1)}.
2. Vì sao baseline không phục hồi? Chưa có checker với threshold và quy trình failover chạy khi primary mất readiness; kết quả NO_RECOVERY ở `reports/measure-drill-1.json:1`.
3. Vì sao không thể chuyển ngay sang B? B sống nhưng bắt đầu `count=0`, `weights=false`, pool warm; evidence `reports/setup-evidence.json:1`. Liveness không đủ để serve inference.
4. Vì sao standby không có state sẵn? Thiết kế ban đầu cố ý chưa có chu kỳ backup/restore vectors và weights/version. Backup phải được chạy trước outage; evidence chu kỳ thực tế `reports/replication.jsonl:1` và restore {ref('reports/failover-events.jsonl', restore)}.
5. Vì sao gap này chưa được phát hiện trước drill? Trạng thái khởi đầu chưa có readiness gate, runbook xác nhận và evidence end-to-end. Giải pháp là kiểm tra khả năng phục hồi thường xuyên và chặn cutover trước ready; không dựa vào việc process đã chạy.

## 4. Action items — owner và deadline

| # | Action item | Owner | Deadline (Asia/Saigon) | Kết quả cần kiểm chứng |
|---|---|---|---|---|
| 1 | So sánh interval 5s với 1s qua 5 drill, giữ threshold 3, đo false positives và RTO. | Lê Văn Sang / on-call | 2026-10-10 | Budget detect giảm lý thuyết 12s; chỉ nhận mức giảm thực tế từ logs. |
| 2 | Thử replication mỗi 10s, kiểm tra DB snapshot nhất quán và version tương thích. | Lê Văn Sang / state owner | 2026-10-11 | Budget chu kỳ giảm 20s; báo cả RPO seconds và docs_lost từng run. |
| 3 | Thêm kiểm tra định kỳ backup tồn tại, standby readiness và drill failover; lưu raw evidence. | Lê Văn Sang / CI owner | 2026-10-12 | Không cutover khi target chưa ready; snapshot lỗi phải ABORT; tránh gap không được quan sát. |
| 4 | Đánh giá standby full-ready so với warm standby và bổ sung quy trình đối soát writes trước failback. | Incident commander + Lê Văn Sang | 2026-10-12 | Có thể giảm warm-up khoảng 6s, đánh đổi chi phí; không cam kết trước khi đo. |

## 5. Ba câu hỏi bắt buộc và reflection

1. Interval × threshold = {floor:.1f}s, bằng {pct:.1f}% RTO. Detection thực tế {components[0]:.4f}s, bằng {actual_pct:.1f}% RTO; không thay số đo bằng budget.
2. Hạ interval xuống 1s với threshold 3 làm budget từ 15s xuống 3s, giảm lý thuyết 12s. RTO thực tế còn phụ thuộc timeout, lịch poll, xác nhận và TTL nên cần chạy lại. Đánh đổi: probe thường hơn và dễ xác nhận outage do transient network kéo dài, cần đánh giá flapping/false positives.
3. Nếu A mất dữ liệu vĩnh viễn trong outage 6 giờ, {docs} docs là lượng primary có nhưng B thiếu tại restore của drill này; không phải ước tính tổng mất dữ liệu của một outage 6 giờ thật. Với khách hàng, đó có thể là ticket/tri thức mới chưa truy xuất được; cần đối soát nguồn, replay ingest và thông báo phạm vi ảnh hưởng. Trong lab, DB A còn trên đĩa nên docs_lost không đồng nghĩa mất vĩnh viễn.

Checker là process riêng, không import serving; API A bị SIGSTOP không dừng checker. Muốn chứng minh mục tiêu 5 phút, dùng traffic JSONL cùng chaos/health/failover logs và phép đo `tools/measure_rto.py`. Có thể giảm warm-up bằng standby ready hoặc giảm TTL trước khi hạ threshold; mỗi phương án cần đo chi phí và tác động.

## 6. Điều đã làm tốt và giới hạn

Threshold chống flapping, xác nhận y/N mặc định, giữ thứ tự 5 bước và chặn readiness timeout. Runbook gọi failover một lần, đọc lại kết quả ở step 4/5, rồi kiểm tra 10 inference thật. Logs có timestamps thật, version model, RPO và docs_lost; không double outage. Backend fs và SQLite cùng máy chỉ mô phỏng multi-region; warm-up là thời gian giả lập, không phải GPU thật. Ingest chạy riêng vẫn ghi A trong lúc API paused; RPO được chốt tại restore. Các tests/serving/chaos/measurement có sẵn được giữ nguyên.
"""
    (ROOT / "reports/postmortem.md").write_text(postmortem, encoding="utf-8")
    print(json.dumps({"rto_s": rto, "rpo_s": rpo, "docs_lost": docs,
                      "detection_s": components[0], "evidence_written": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
