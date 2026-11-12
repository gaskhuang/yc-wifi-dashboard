#!/usr/bin/env python3
"""單台 AP 的逐台裝置追蹤:每分鐘記錄快照,狀態變化時寫事件

用法:python3 ap_watch.py 2        # 追蹤 AP 2
  - 快照  → logs/ap<名>-日期.jsonl(跟 logs/ 一起同步到 NAS)
  - 事件  → logs/ap<名>-events.log(連上/離開、訊號弱、滿意度低、速率低、AP 本身異常;預設每 3 分鐘一行摘要(AP_WATCH_SUMMARY 秒))
UniFi 本機帳號讀 ~/.unifi-monitor.yaml(同 unifi-monitor),只讀不寫。
"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta
import httpx, yaml

AP = sys.argv[1] if len(sys.argv) > 1 else "2"
SITE = "x9ajowek"
EVERY = 60
SUMMARY_EVERY = int(os.environ.get("AP_WATCH_SUMMARY", "180"))   # 摘要間隔(秒)
WEAK_DBM, LOW_SAT, LOW_RATE = -75, 80, 12      # 與儀表板判讀一致
RADIO_RETRY, RADIO_AIR = 20, 60
TPE = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
EVENTS = os.path.join(LOGDIR, f"ap{AP}-events.log")

cfg = yaml.safe_load(open(os.path.expanduser("~/.unifi-monitor.yaml")))["local_controllers"][0]
BASE = cfg["base_url"] + "/proxy/network"


class Client:
    def __init__(self):
        self.c = httpx.Client(verify=False, timeout=30)
        self.h = {}

    def login(self):
        r = self.c.post(cfg["base_url"] + "/api/auth/login",
                        json={"username": cfg["username"], "password": cfg["password"]})
        r.raise_for_status()
        self.h = {"X-CSRF-Token": r.headers.get("x-csrf-token")}

    def get(self, path):
        r = self.c.get(BASE + path, headers=self.h)
        if r.status_code in (401, 403):
            self.login()
            r = self.c.get(BASE + path, headers=self.h)
        r.raise_for_status()
        return r.json()["data"]


def now():
    return datetime.now(TPE)


def event(msg):
    line = f"{now():%H:%M:%S} AP{AP} {msg}"
    with open(EVENTS, "a") as f:
        f.write(line + "\n")


def label(s):
    return f"{s.get('hostname') or s.get('oui') or s['mac'][-5:]}({s.get('essid')},{s.get('radio')})"


def problems(s):
    out = []
    sig, sat, rate = s.get("signal") or 0, s.get("satisfaction"), (s.get("rx_rate") or 0) / 1000
    if sig and sig < WEAK_DBM:
        out.append(f"訊號 {sig}dBm")
    if sat is not None and 0 <= sat < LOW_SAT:
        out.append(f"滿意度 {sat}")
    if 0 < rate < LOW_RATE:
        out.append(f"速率 {rate:.0f}M")
    return out


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    api = Client()
    api.login()
    seen, bad, radio_bad = {}, {}, set()
    last_summary = 0
    joined = left = 0
    event("開始追蹤")
    while True:
        try:
            devs = api.get(f"/api/s/{SITE}/stat/device")
            ap = next((d for d in devs if d.get("name") == AP), None)
            if not ap:
                event("找不到這台 AP")
                time.sleep(EVERY)
                continue
            if ap.get("state") != 1:
                if "offline" not in radio_bad:
                    event(f"⚠ AP 離線(state {ap.get('state')})")
                radio_bad.add("offline")
            elif "offline" in radio_bad:
                radio_bad.discard("offline")
                event("AP 恢復在線")
            radios = {x["radio"]: x for x in ap.get("radio_table_stats", []) if x["radio"] != "ng"}
            for rn, x in radios.items():
                issue = (x.get("tx_retries_pct") or 0) >= RADIO_RETRY or (x.get("cu_total") or 0) >= RADIO_AIR
                if issue and rn not in radio_bad:
                    event(f"⚠ {rn} ch{x.get('channel')} 重送 {x.get('tx_retries_pct')}% 空中時間 {x.get('cu_total')}%")
                    radio_bad.add(rn)
                elif not issue and rn in radio_bad:
                    event(f"{rn} 恢復正常(重送 {x.get('tx_retries_pct')}% 空中時間 {x.get('cu_total')}%)")
                    radio_bad.discard(rn)
            sta = [s for s in api.get(f"/api/s/{SITE}/stat/sta") if s.get("ap_mac") == ap["mac"]]
            cur = {s["mac"]: s for s in sta}
            for mac, s in cur.items():
                if mac not in seen:
                    joined += 1
                    event(f"+ 連上 {label(s)} 訊號 {s.get('signal')}")
                p = problems(s)
                if p and not bad.get(mac):
                    event(f"⚠ {label(s)} " + "、".join(p))
                elif not p and bad.get(mac):
                    event(f"✓ {label(s)} 恢復(訊號 {s.get('signal')} 滿意度 {s.get('satisfaction')})")
                bad[mac] = bool(p)
            for mac in set(seen) - set(cur):
                left += 1
                event(f"- 離開 {label(seen[mac])}")
                bad.pop(mac, None)
            seen = cur
            # 快照:每台裝置的關鍵欄位
            snap = {"ts": f"{now():%Y-%m-%d %H:%M:%S}", "ap": AP, "state": ap.get("state"),
                    "radios": {rn: {k: x.get(k) for k in ("channel", "tx_power", "cu_total", "cu_self_rx", "cu_self_tx",
                                                          "num_sta", "tx_retries_pct")} for rn, x in radios.items()},
                    "clients": [{k: s.get(k) for k in ("mac", "hostname", "oui", "essid", "radio", "channel", "signal",
                                                       "noise", "tx_rate", "rx_rate", "satisfaction", "tx_retries",
                                                       "tx_bytes-r", "rx_bytes-r", "uptime", "ip")} for s in sta]}
            with open(os.path.join(LOGDIR, f"ap{AP}-{now():%Y-%m-%d}.jsonl"), "a") as f:
                f.write(json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n")
            if time.time() - last_summary >= SUMMARY_EVERY:
                issues = [f"{label(s)} " + "、".join(problems(s)) for s in cur.values() if problems(s)]
                rs = " ".join(f"{rn} ch{x.get('channel')} 空中{x.get('cu_total')}% 重送{x.get('tx_retries_pct')}%"
                              for rn, x in radios.items())
                event(f"摘要:{len(cur)} 台裝置(+{joined}/-{left}),{len(issues)} 台有狀況 | {rs}"
                      + ("".join(f"\n    ⚠ {i}" for i in issues)))
                last_summary = time.time()
                joined = left = 0
        except Exception as e:   # 網路或 controller 暫時性錯誤:記一筆、下輪再試
            event(f"讀取失敗:{type(e).__name__} {str(e)[:80]}")
            try:
                api.login()
            except Exception:
                pass
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
