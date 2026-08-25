# RTO/RPO Evidence — Lab 23 (drill ngày 2026-08-25)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T08:36:45` | chaos kill (`netblock` = SIGSTOP region-a) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage (`ReadTimeout`, ~2009ms ≈ edge timeout 2s) | `reports/drill-1-nodr.jsonl:53` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage trong toàn bộ file | `reports/drill-1-nodr.jsonl` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300` → `rto_verdict:"NO_RECOVERY"`, `requests_failed:6`, `valid:true` | `reports/drill-1-nodr.jsonl` |

Kết luận drill 1: không health check, không failover → hệ thống down vĩnh viễn cho tới khi
có người chạy tay `kill_region.py restore`.

## 2. Drill 2 — có DR

t_outage = ts `1787649533.683` (mốc 0). Toàn bộ "+giây" tính từ mốc này.

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage (mốc 0) | 0 | `action:kill`, `mode:netblock`, region a, `other_alive:true` | `chaos/chaos-events.jsonl:9` |
| User thấy lỗi đầu tiên | 0.0 | dòng `ok:false` đầu (`ReadTimeout`) — request đang treo đúng lúc SIGSTOP | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 21.9 | `to:UNHEALTHY, region:a`, reason `ReadTimeout`, sau 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 15.9 | `step:2_restore_snapshot`, `rpo_seconds:12.76`, `docs_lost:5` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 22.0 | `step:4_wait_ready`, `ready:true` | `reports/failover-events.jsonl:4` |
| DNS cutover | 22.0 | `step:5_dns_cutover`, `active_region:b` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **22.9** | dòng `ok:true` đầu sau lỗi, `served_by:"b"` | `reports/drill-2-withdr.jsonl:35` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `22.9s` | 300s (5 phút) | PASS (`tools/measure_rto.py`: `valid:true`, `warnings:[]`) |
| RPO — Vector DB | `12.76s` / `5` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detect floor | 15.0 | `interval_s(5) × threshold(3)` ghi trong `reports/health-events.jsonl:2`; detect thực tế +21.9s vì probe thứ 3 bắt đầu sau t_outage và phải treo hết timeout 2s | Hạ `--interval` hoặc `--threshold` — nhưng đánh đổi bằng nguy cơ flapping (§4) |
| Snapshot restore | 0.0 | `2_restore_snapshot` xong tại `+15.9s` (`reports/failover-events.jsonl:2`), nằm gọn trong cửa sổ detect floor; fs backend chỉ copy 2 file | fs gần như free; MinIO/S3 thật sẽ cộng network latency vào đây |
| GPU pool warm-up | 6.1 | `3_scale_pool` tại `+15.9s` (`reports/failover-events.jsonl:3`) → `4_wait_ready` tại `+22.0s` (`reports/failover-events.jsonl:4`) — đúng `WARMUP_SECONDS=6` của `serving/app.py` | Giảm WARMUP_SECONDS thật ra là giữ pool sẵn nóng (pre-warmed standby), đổi lại tốn chi phí GPU |
| DNS/LB TTL cache | 0.9 | t_recovered `+22.9s` − t_cutover `+22.0s` (`reports/drill-2-withdr.jsonl:35` vs `reports/failover-events.jsonl:5`) — request cuối cùng còn trúng cache cũ trỏ về a | Giảm EDGE_TTL_SECONDS; trade-off: edge đọc "DNS record" thường hơn |

Tổng trên timeline: detect 21.9 + restore/scale (song song trong cửa sổ detect) +
warm-up 6.1 + TTL 0.9 ≈ 22.9s = RTO đo được. Detect floor lý thuyết 15.0s, run này
detect ở +21.9s vì chu kỳ poll lệch pha với t_outage (probe #1 trước kill không đếm,
probe #2/#3 mỗi cái treo đủ timeout 2s của netblock).

## 4. Cấu hình health checker của run này

`--interval 5 --threshold 3 --timeout 2.0` (mặc định theo GUIDE) → detect floor 15.0s,
thực tế +21.9s do pha lệch chu kỳ + timeout treo. Trả lời câu hỏi trong docstring:
với RTO mục tiêu 300s, detect floor ≤ ~50% RTO là hợp lý, tức `interval × threshold ≤
150s` — cấu hình hiện tại dư địa rất rộng, có thể hạ interval xuống 1–2s nếu muốn RTO
còn thấp hơn mà chưa cần đụng vào flapping.
