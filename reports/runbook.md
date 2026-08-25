# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong. Region chính mặc định là `a`, region phụ `b`.

Điều kiện chạy: stack bare mode đang lên (`bash scripts/up_bare.sh`), snapshot tồn tại
(`state/_replica/dr-artifacts/MANIFEST.json` có sẵn từ chu kỳ replicate). Nếu chưa có
snapshot: chạy `python3 state/replicate.py --every 30 --duration 60 --backend fs` và
chờ ít nhất 1 chu kỳ trước khi tới bước 3.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` — chạy lại 3 lần cách nhau ~5s | `"a": {"alive": false}` ở cả 3 lần (đừng tin 1 lần fail) | on-call |
| 2 | Mở incident + bấm giờ RTO | `date -Is` ghi vào incident channel; hoặc để automation tự ghi: `python3 dr/runbook.py --primary a --target b --backend fs` (hỏi y/N) | ts xuất hiện trong `reports/runbook-run.jsonl` bước 2 `thong_bao_incident` | on-call (incident commander) |
| 3 | Restore state ở region phụ | `python3 dr/runbook.py ...` gọi hộ; tay thuần: `python3 state/snapshot.py get --region b --backend fs` | `reports/failover-events.jsonl` có dòng `2_restore_snapshot` với `rpo_seconds`, `docs_lost`, `embed_model_version` | on-call |
| 4 | Scale pool warm→full | nằm trong runbook auto; tay thuần: `echo full > state/region-b/pool_state` rồi poll | `/readyz` của b trả 200: `curl -s localhost:8002/readyz \| grep '"ready":true'` | on-call |
| 5 | DNS/LB cutover | nằm trong runbook auto; tay thuần: `printf b > edge/active_region` | `curl localhost:8080/edge/state` cho `active_region:b`; dòng `5_dns_cutover` trong failover-events | on-call |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -s "localhost:8080/v1/infer?q=t$i"; done` | ≥ 9/10 response có `"answer":"[b] ..."`, p95 < 500ms, error rate = 0 (runbook auto in `p95_latency_ms`, `error_rate`) | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` != null, `valid:true`, `warnings:[]`; mở incident channel dán output | incident commander |

**Một lệnh duy nhất cho người hoảng loạn lúc 3h sáng** (làm hộ bước 1→7 trừ bước xác
nhận tay): `python3 dr/runbook.py --primary a --target b --backend fs` — đọc y/N,
script tự verify từng bước và dừng nếu target chưa ready (không bao giờ cutover mù).

**Rollback (failover ngược):**
- Điều kiện rollback về region A: A đã sống lại (`kill_region.py status` → `a.ready:true`)
  **và** dữ liệu đã bắt kịp (`snapshot.py put --region a` rồi so `lag`) **và** cửa sổ
  traffic thấp (tránh cutover giữa giờ cao điểm).
- Trình tự: y hệt bước 3–5 nhưng `--primary b --target a`.
- Ai quyết định: **incident commander** (người mở incident ở bước 2), không phải ai
  nhìn thấy A sống cũng được flip — §4 Anti-Patterns: full-auto failover không circuit
  breaker gây flapping hai chiều. Tối thiểu 15 phút ổn định ở B trước khi xét rollback.
