"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(step: str, **kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout.

    Log này là nguồn t_cutover (bước 5) và rpo_seconds/docs_lost (bước 2)
    cho tools/measure_rto.py — thiếu field thì mất điểm.
    """
    line = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step": step, "target": kw.pop("target", None), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(line) + "\n")
    print(json.dumps(line))
    return line


def state_of(region: str) -> dict:
    """/v1/state của một region — dùng ở 1_verify_target và khi verify sau restore."""
    return httpx.get(f"{URL[region]}/v1/state", timeout=2).json()


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Cutover là bước CUỐI, chỉ làm khi target ready."""
    result = {"ok": False, "target": target, "backend": backend}

    # 1_verify_target — trạng thái hiện tại của region phụ (thường là chưa sẵn sàng).
    try:
        st = state_of(target)
    except Exception as e:
        st = {"region": target, "error": type(e).__name__}
    emit("1_verify_target", target=target, state=st)

    # 2_restore_snapshot — kéo snapshot mới nhất về target + đo RPO thật.
    meta = snapshot.get(target, backend)
    rpo = snapshot.rpo(pathlib.Path("state/region-a/vectors.sqlite"),
                       pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit("2_restore_snapshot", target=target,
         rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
         embed_model_version=meta.get("embed_model_version"))
    result.update(rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
                  embed_model_version=meta["embed_model_version"])

    # 3_scale_pool — warm -> full; serving/app.py bắt đầu đếm WARMUP_SECONDS từ đây.
    pathlib.Path(f"state/region-{target}/pool_state").write_text("full")
    emit("3_scale_pool", target=target, pool_state="full")

    # 4_wait_ready — poll /readyz tới khi 200. Timeout -> ABORT, KHÔNG cutover:
    # đổi DNS trước khi target sẵn sàng = 503 từ CẢ HAI region.
    deadline = time.time() + wait
    while True:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=2)
            ready = (r.status_code == 200)
            reasons = r.json().get("reasons", [])
        except Exception as e:  # server đang boot / bị mock ConnectError trong test
            ready, reasons = False, [type(e).__name__]
        if ready:
            break
        if time.time() >= deadline:
            emit("abort_no_cutover", target=target,
                 reason="target khong ready trong wait", last_reasons=reasons)
            return result  # ok vẫn False — không chạm vào edge/active_region
        time.sleep(0.5)
    emit("4_wait_ready", target=target, ready=True)

    # 5_dns_cutover — bước cuối cùng: ghi region đích vào "DNS record".
    pathlib.Path("edge/active_region").write_text(target)
    emit("5_dns_cutover", target=target, active_region=target)

    # Verify lại bằng chính API công khai của target (không tin theo log nội bộ).
    final = state_of(target)
    result.update(ok=True, pool_state=final.get("pool_state"),
                  vectors=final.get("count"), weights=final.get("weights"))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
