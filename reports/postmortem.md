# Postmortem — DR Drill Lab 23 (2026-08-25, run chuẩn cuối)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

Mốc 0: t_outage = `2026-08-25T09:18:53` (ts `1787649533.683`).

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 09:18:53 | outage bắt đầu — chaos kill region-a (`netblock`/SIGSTOP) | `chaos/chaos-events.jsonl:9` |
| 09:18:53 | user đầu tiên bị ảnh hưởng — request đang treo ăn ReadTimeout ngay tại +0.0s | `reports/drill-2-withdr.jsonl:25` |
| 09:19:15 | health check alert — region-a UNHEALTHY sau 3 fail liên tiếp (+21.9s) | `reports/health-events.jsonl:2` |
| 09:19:09–15 | operator confirm cutover — runbook auto: xác nhận outage → gọi failover 5 bước | `reports/runbook-run.jsonl:1` |
| 09:19:15 | resolved (request đầu tiên OK từ region-b, +22.9s) | `reports/drill-2-withdr.jsonl:35` |

Độ trễ thông báo: operator (runbook automation) biết tin lúc ts `1787649549.552`
(`reports/runbook-run.jsonl:2`) = t_outage + 15.9s — đúng bằng detect floor
`interval × threshold = 15s` cộng một chu kỳ poll.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `22.9s` · gap: `-277.1s` (tốt hơn mục tiêu)
- RPO mục tiêu: 300s · đo được: `12.76s` (`5` doc bị mất) · gap: `-287.2s`
- **Bước tốn nhiều giây nhất:** health-check detection (+21.9s trên tổng 22.9s RTO,
  ~96%) — vì sao: detect floor là `interval_s × threshold = 5 × 3 = 15s`, cộng thêm
  pha lệch chu kỳ poll và mỗi probe phải treo hết timeout 2s trước khi tính là fail.
  Snapshot restore gần như 0s (fs backend), warm-up cố định ~6.1s, TTL cache 0.9s.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. Vì sao user thấy lỗi? → Region A ngừng phục vụ mà edge vẫn forward theo DNS cache cũ.
2. Vì sao edge vẫn forward về A? → Edge không có cơ chế tự phát hiện region chết;
   nó chỉ đọc `edge/active_region`, và cache TTL 5s khiến cutover muộn thêm.
3. Vì sao không ai chuyển traffic sớm hơn? → Không có health checker chạy thường trực:
   lần đầu drill (không DR) hệ thống ở trạng thái NO_RECOVERY vĩnh viễn
   (`reports/drill-1-nodr.jsonl`). Health checker chỉ tồn tại từ Step 3.
4. Vì sao detection chiếm ~96% RTO? → Tham số chống flapping `interval=5, threshold=3`
   đặt detection floor 15s; đây là lựa chọn chủ đích để một blip mạng đơn lẻ không gây
   failover nhầm — cái giá của sự chắc chắn.
5. Vì sao RPO = 12.76s/5 docs thay vì 0? → Replication chạy chu kỳ 30s; mọi doc ingest
   giữa 2 chu kỳ nằm ngoài snapshot mới nhất. Đây là lag có cấu trúc của async replication,
   không phải bug — muốn giảm phải đổi sang sync replication và trả giá bằng latency ghi.

Root cause tổng: hệ thống ban đầu thiếu cả 3 lớp tự cứu (health check, failover tự động,
replication) — "process còn sống" bị nhầm là "region usable". Sau lab, cả 3 lớp đã có
và được đo bằng timestamp thật.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ `--interval` health checker xuống 2s (giữ threshold 3): floor 15s → 6s | Platform on-call | Tuần sau | RTO −~12s (detect 21.9 → ~8–10s) |
| 2 | Rút ngắn chu kỳ replicate 30s → 10s hoặc thêm incremental log shipping | Data platform | Tuần sau | RPO −~20s trung bình (lag giảm ⅔) |
| 3 | Pre-warm pool region phụ ở `warm` thay vì `cold` để bỏ 6.1s WARMUP_SECONDS khỏi RTO | Infra | Tháng sau | RTO −6.1s, đánh đổi chi phí compute đứng chờ |
| 4 | Cho edge probe `/readyz` upstream trước khi forward (active health check ở LB) | Platform | Tháng sau | Cắt 0.9s TTL cache + loại bỏ 503 tới user trong cửa sổ cache |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
   → 5s × 3 = **15s** detect floor; thực tế detect mất 21.9s (do lệch pha + timeout treo),
   tức ~**96%** của RTO 22.9s. Detection là thành phần áp đảo.
2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
   → Floor rơi từ 15s xuống 3s, RTO thực tế dự kiến giảm ~13–18s. Cái giá: mỗi blip mạng
   thoáng qua cũng đếm nhanh tới ngưỡng 3 fail liên tiếp, tăng xác suất failover nhầm;
   nếu kèm auto-failback không có circuit breaker thì 2 region flap qua lại — đúng anti-
   pattern §4. Đánh đổi chấp nhận được nếu giữ threshold ≥ 3 và yêu cầu confirm của
   người vận hành trước khi cutover (như thiết kế bán-tự động của runbook).
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
   bạn có nghĩa gì với khách hàng?
   → Với khách hàng, `docs_lost` không phải "5 dòng trong DB" — đó là những ticket/hỏi đáp
   khách đã gửi và *tin là đã được ghi nhận* nhưng biến mất: họ sẽ phải hỏi lại, và câu
   trả lời mô hình đưa ra sau failover thiếu ngữ cảnh đó. RPO 12.76s nghĩa là cam kết
   "tối đa mất ~13 giây hoạt động"; nếu SLA kinh doanh đòi hỏi 0 mất mát thì bắt buộc
   chuyển sang replication đồng bộ cho phần ghi trước khi trả ACK cho khách.
