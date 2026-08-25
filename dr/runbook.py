"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    line = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(line) + "\n")
    print(json.dumps(line))
    return line


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước ở trên. Gọi failover() ĐÚNG MỘT LẦN ở bước 3."""
    out = {"primary": primary, "target": target, "ok": False}
    if not confirm(auto, f"Xac nhan failover {primary} -> {target}?"):
        step(0, "huy_bo", reason="operator tu choi confirm")
        out["reason"] = "operator_cancelled"
        return out

    # 1 — xác nhận outage bằng nhiều lần probe, đừng tin 1 lần fail.
    probes = [health_checker.probe(primary, 2.0)[0] for _ in range(3)]
    primary_down = not any(probes)
    step(1, "xac_nhan_outage", region=primary, probes_ready=probes,
         confirmed=primary_down)

    # 2 — mở incident + bấm giờ RTO (ts của dòng này = mốc operator biết tin).
    t_incident = time.time()
    step(2, "thong_bao_incident", t_outage_ref="chaos/chaos-events.jsonl",
         incident_ts=t_incident)

    # 3 — gọi failover() MỘT LẦN DUY NHẤT; nó tự làm đủ 5 bước con và tự ghi log.
    fo_result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", failover_ok=fo_result.get("ok"),
         target=target, backend=backend)

    # 4-5 — chỉ ĐỌC lại kết quả từ dict bước 3 trả về, không gọi lại failover.
    st = fo_result.get("vectors"), fo_result.get("weights")
    step(4, "verify_state_replica", target=target,
         vector_count=st[0], weights_ok=st[1],
         rpo_seconds=fo_result.get("rpo_seconds"),
         docs_lost=fo_result.get("docs_lost"))
    step(5, "dns_cutover", active_region=target,
         ok=fo_result.get("ok"))

    # 6 — golden signals: 10 request thật vào region phụ, p95 + error rate.
    lat, errs = [], 0
    for i in range(10):
        try:
            r = httpx.get(URL[target] + "/v1/infer",
                          params={"q": f"golden check {i}"}, timeout=5)
            lat.append(r.elapsed.total_seconds() * 1000)
            if r.status_code != 200:
                errs += 1
        except Exception:
            errs += 1
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else None
    err_rate = errs / 10
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=p95,
         error_rate=err_rate)
    out.update(p95_latency_ms=p95, error_rate=err_rate)

    # 7 — tổng kết sau incident + lệnh đo RTO.
    elapsed = round(time.time() - t_incident, 1)
    step(7, "post_incident", elapsed_s=elapsed,
         measure_cmd=f"python3 tools/measure_rto.py --loadgen "
                     f"reports/drill-2-withdr.jsonl --target-rto 300")
    out.update(ok=fo_result.get("ok", False), elapsed_s=elapsed,
               failover=fo_result)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
