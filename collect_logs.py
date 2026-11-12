#!/usr/bin/env python3
"""活動紀錄收集:每分鐘把 UniFi 與 MikroTik 的紀錄存到本機 logs/,供活動後分析

每分鐘(由 auto-update.sh 呼叫):
  - UniFi 系統紀錄(連線、斷線、漫遊、AP 事件)    → logs/unifi-syslog-日期.jsonl
  - RB5009 紀錄(DHCP、PPPoE、系統;防火牆只計數)→ logs/ccr-log-日期.jsonl
  - 每位無線用戶當下的狀態快照                    → logs/clients-日期.jsonl
每 5 分鐘:
  - RB5009 的 DHCP 租約表快照                         → logs/dhcp-日期.jsonl
每小時(以及執行 python3 collect_logs.py export 時):
  - UniFi 連線紀錄(session)、5 分鐘/小時報表、歷史用戶 → logs/export/

紀錄含與會者裝置 MAC 等個人資料,只存在本機,不進 repo、不上儀表板。
"""
import base64, json, os, ssl, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

HOST = "https://m09829.hostifi.com:11443/proxy/network"
SITE = "x9ajowek"
API_KEY = os.environ.get("YC_UNIFI_API_KEY", "")
MT_USER = os.environ.get("YC_NOC_USER", "")
MT_PASS = os.environ.get("YC_NOC_PASS", "")
CCR_HOSTS = [h for h in os.environ.get("YC_CCR_HOSTS",
             "http://hgk09ry2t9c.sn.mynetname.net:8089").split(",") if h]
EVENT_START = os.environ.get("YC_EVENT_START", "2026-11-11 06:00:00")   # 匯出報表的起點

TPE = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
STATE = os.path.join(LOGDIR, "collect_state.json")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def unifi(path, body=None):
    req = urllib.request.Request(HOST + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-API-KEY": API_KEY, "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ccr(path):
    auth = "Basic " + base64.b64encode(f"{MT_USER}:{MT_PASS}".encode()).decode()
    for base in CCR_HOSTS:
        try:
            req = urllib.request.Request(base + path, headers={"Authorization": auth})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
    return None


def append(name, rows, day):
    if not rows:
        return
    with open(os.path.join(LOGDIR, f"{name}-{day}.jsonl"), "a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def collect(st):
    now = time.time()
    ts = datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S")
    day = ts[:10]
    out = {}

    # UniFi 系統紀錄:從上次讀到的時間點往後抓
    since = st.get("unifi_since", int((now - 600) * 1000))
    new, page, newest = [], 0, since
    while page < 20:
        r = unifi(f"/v2/api/site/{SITE}/system-log/all",
                  {"timestampFrom": since + 1, "timestampTo": int(now * 1000), "pageNumber": page, "pageSize": 200})
        items = r.get("data", [])
        new += items
        newest = max([newest] + [int(i.get("timestamp") or 0) for i in items])
        if len(items) < 200:
            break
        page += 1
    append("unifi-syslog", new, day)
    st["unifi_since"] = newest
    out["unifi_syslog"] = len(new)

    # RB5009 紀錄:記憶體只留 1000 行,用 .id 去重;防火牆擋掉的外部掃描量太大,只計數
    log = ccr("/rest/log")
    if log is not None:
        last = st.get("ccr_last_id", "")
        def key(x):  # .id 形如 *1A2B,換成數字比較先後
            try:
                return int(x.get(".id", "*0")[1:], 16)
            except ValueError:
                return 0
        fresh = [x for x in log if key(x) > (int(last[1:], 16) if last else -1)]
        fw = [x for x in fresh if "firewall" in (x.get("topics") or "")]
        keep = [dict(x, collected=ts) for x in fresh if x not in fw]
        append("ccr-log", keep, day)
        append("ccr-firewall-count", [{"ts": ts, "dropped_logged": len(fw)}], day)
        if log:
            st["ccr_last_id"] = max(log, key=key).get(".id")
        # 這一分鐘新增的紀錄比緩衝區還多,代表有漏掉
        out["ccr_log"] = len(keep)
        out["ccr_maybe_lost"] = bool(last) and len(fresh) >= len(log)

    # 每位無線用戶的狀態快照
    sta = unifi(f"/api/s/{SITE}/stat/sta").get("data", [])
    snap = [{k: c.get(k) for k in ("mac", "hostname", "oui", "essid", "ap_mac", "radio", "channel", "signal",
                                   "tx_rate", "rx_rate", "satisfaction", "tx_retries", "wifi_tx_attempts",
                                   "uptime", "tx_bytes", "rx_bytes", "ip")}
            for c in sta if not c.get("is_wired")]
    append("clients", [{"ts": ts, "clients": snap}], day)
    out["clients"] = len(snap)

    # 每 5 分鐘:DHCP 租約表
    if now - st.get("dhcp_at", 0) >= 300:
        leases = ccr("/rest/ip/dhcp-server/lease")
        if leases is not None:
            append("dhcp", [{"ts": ts, "leases": [{k: l.get(k) for k in ("address", "mac-address", "host-name",
                                                                          "status", "expires-after", "last-seen")}
                                                   for l in leases]}], day)
            st["dhcp_at"] = now
    return out


def export():
    """匯出 UniFi 的歷史資料(連線紀錄、報表、歷史用戶)。UniFi 只保留一段時間,活動期間每小時匯出一次"""
    start = int(datetime.strptime(EVENT_START, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE).timestamp())
    end = int(time.time())
    d = os.path.join(LOGDIR, "export")
    os.makedirs(d, exist_ok=True)
    jobs = {
        "sessions": (f"/api/s/{SITE}/stat/session", {"type": "all", "start": start, "end": end}),
        "alluser": (f"/api/s/{SITE}/stat/alluser", {"type": "all", "within": max(1, (end - start) // 3600 + 1)}),
    }
    for kind, attrs in {"5minutes.ap": ["num_sta", "bytes", "tx_bytes", "rx_bytes", "time"],
                        "5minutes.user": ["rx_bytes", "tx_bytes", "time"],
                        "5minutes.site": ["num_sta", "wan-rx_bytes", "wan-tx_bytes", "time"],
                        "hourly.ap": ["num_sta", "bytes", "time"],
                        "hourly.site": ["num_sta", "wan-rx_bytes", "wan-tx_bytes", "time"]}.items():
        jobs["report-" + kind] = (f"/api/s/{SITE}/stat/report/{kind}",
                                  {"attrs": attrs, "start": start * 1000, "end": end * 1000})
    n = {}
    for name, (path, body) in jobs.items():
        try:
            data = unifi(path, body).get("data", [])
            json.dump(data, open(os.path.join(d, f"{name}.json"), "w"), ensure_ascii=False)
            n[name] = len(data)
        except Exception as e:
            n[name] = f"失敗 {str(e)[:60]}"
    return n


def main():
    if not API_KEY:
        print("collect: 沒有 UniFi API 金鑰,略過")
        return
    os.makedirs(LOGDIR, exist_ok=True)
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        print("collect: 匯出", export())
        return
    out = collect(st)
    if time.time() - st.get("export_at", 0) >= 3600:
        out["export"] = export()
        st["export_at"] = time.time()
    json.dump(st, open(STATE, "w"))
    print("collect:", out)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # 收集失敗不影響儀表板更新
        print("collect: 失敗", type(e).__name__, str(e)[:120])
