"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        body = r.json()
        # /readyz 200 khi ready; khi 503 body vẫn có danh sách reasons cụ thể.
        if r.status_code == 200:
            return True, "ok"
        return False, ";".join(body.get("reasons", [f"http_{r.status_code}"]))
    except Exception as e:  # netblock -> ReadTimeout; SIGKILL -> ConnectError
        return False, type(e).__name__


def run(interval: float, timeout: float, threshold: int, duration: float,
        out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out.parent.mkdir(parents=True, exist_ok=True)
    # state giữ per-region: trạng thái hiện tại + chuỗi fail liên tiếp.
    state = {r: {"status": None, "fails": 0} for r in URL}
    t_end = time.time() + duration
    with out.open("a") as f:
        while time.time() < t_end:
            for region in sorted(URL):
                ready, reason = probe(region, timeout)
                s = state[region]
                if ready:
                    s["fails"] = 0
                    # Trạng thái đầu tiên KHÔNG ghi log — chỉ log khi có sự CHUYỂN
                    # trạng thái thật (UNHEALTHY -> HEALTHY), tránh nhiễu lúc boot.
                    s["status"] = "HEALTHY"
                else:
                    s["fails"] += 1
                    # Chỉ flip UNHEALTHY sau threshold fail LIÊN TIẾP — một lần
                    # blip mạng không phải outage (chống flapping §4).
                    if s["fails"] >= threshold and s["status"] != "UNHEALTHY":
                        s["status"] = "UNHEALTHY"
                        _emit(f, region, "UNHEALTHY", reason, interval, threshold)
            time.sleep(interval)


def _emit(f, region: str, to: str, reason: str, interval: float, threshold: int):
    """Ghi đúng 1 dòng khi trạng thái ĐỔI — đây là nguồn t_detect của measure_rto."""
    line = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "state_change", "region": region, "to": to, "reason": reason,
            "consecutive_fails": threshold if to == "UNHEALTHY" else 0,
            "interval_s": interval, "threshold": threshold}
    f.write(json.dumps(line) + "\n")
    f.flush()
    print(json.dumps(line))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
