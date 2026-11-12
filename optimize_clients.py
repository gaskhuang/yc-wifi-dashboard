#!/usr/bin/env python3
"""連線優化:找出「塞住的 AP 上持續連線不佳」的裝置,踢掉讓它重新連到較好的 AP

每分鐘由 auto-update.sh 呼叫一次,做三件事:
  1. 把這一分鐘每台 AP 的狀況寫進 logs/日期.jsonl(活動後可回顧)
  2. 找出連線不佳的裝置並累計連續分鐘數
  3. 符合條件就踢掉(kick-sta),裝置會自動重連,通常會改連訊號較好的 AP

判定規則:
  - 只看「塞住」的 AP:5GHz 空中時間 >= 70%
  - 連線不佳:訊號 < -75 dBm,或「正在用網路」時 AP 對它的速率 < 12 Mbps
  - 剛連上未滿 2 分鐘的不算(速率還在爬升)
  - 連續 3 分鐘都不佳才踢
回報(不論 AP 塞不塞都追蹤):
  - 同一台裝置連續 3 分鐘不佳,算「持續不佳」
  - 全場同時 >= 10 台、同一台 AP >= 5 台,或同一批裝置反覆/長時間不佳 >= 5 台,
    才在儀表板「需要調整的項目」加一則警告(LINE 依 notify.py 規則推播)
安全限制(避免活動中大量斷線):
  - 只處理與會者 SSID(PRO1009),工作人員 SSID 不動
  - 每分鐘最多踢 5 台、同一台 AP 最多 2 台
  - 同一台裝置 15 分鐘內不重複踢;一小時內踢過 2 次仍不佳就不再踢,只記錄
  - 預設是觀察模式(只記錄「會踢誰」),環境變數 OPTIMIZE_MODE=kick 才會真的踢

對外公開的儀表板只顯示遮蔽過的 MAC,完整紀錄留在本機 logs/。
"""
import json, os, ssl, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HOST = "https://m09829.hostifi.com:11443"
SITE = "x9ajowek"
NET = f"{HOST}/proxy/network/api/s/{SITE}"
API_KEY = os.environ.get("YC_UNIFI_API_KEY", "")
MODE = os.environ.get("OPTIMIZE_MODE", "observe")      # observe | kick
GUEST_SSID = "PRO1009"

CONGESTED_AIRTIME = 70
WEAK_DBM = -75
SLOW_MBPS = 12
ACTIVE_BPS = 5000          # 每秒傳輸超過 5 KB 才算「正在用網路」
MIN_UPTIME = 120
STREAK_TO_KICK = 3
COOLDOWN = 15 * 60
MAX_KICKS_PER_HOUR = 2
MAX_PER_ROUND, MAX_PER_AP = 5, 2
# 回報門檻:「很多人同時」或「同一批人一直出現」才回報,個別裝置不回報
GROUP_SITE, GROUP_AP = 10, 5        # 全場同時 >= 10 台,或同一台 AP >= 5 台持續不佳
REPEAT_EPISODES = 3                 # 一小時內第 3 次又持續不佳
LONG_MINUTES = 10                   # 或連續不佳 10 分鐘以上
GROUP_REPEAT = 5                    # 這種「老是出問題」的裝置 >= 5 台

TPE = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "optimize_state.json")
LOGDIR = os.path.join(HERE, "logs")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def call(path, body=None):
    req = urllib.request.Request(
        NET + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-KEY": API_KEY, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def mask(mac):
    return "…" + mac[-5:] if mac else "?"


def main():
    if not API_KEY:
        print("optimize: 沒有 UniFi API 金鑰,略過")
        return
    now = time.time()
    ts = datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S")
    devs = [d for d in call("/stat/device")["data"] if d.get("type") == "uap" and d.get("state") == 1]
    sta = [c for c in call("/stat/sta")["data"] if not c.get("is_wired")]

    # 每台 AP(以 MAC 為準)的最高空中時間;雙 5G 機種取兩個無線電中較高者
    ap_name, ap_air = {}, {}
    for d in devs:
        mac = d.get("mac")
        ap_name[mac] = d.get("name") or mac[-5:]
        ap_air[mac] = max([r.get("cu_total") or 0 for r in d.get("radio_table_stats", [])
                           if r.get("radio") == "na"] or [0])

    try:
        st = json.load(open(STATE))
    except Exception:
        st = {"clients": {}, "recent": []}
    clients = st["clients"]

    candidates, seen = [], set()
    per_ap = {}
    for c in sta:
        mac, ap = c.get("mac"), c.get("ap_mac")
        per_ap.setdefault(ap, {"n": 0, "bad": 0})["n"] += 1
        if c.get("essid") != GUEST_SSID or c.get("radio") != "na":
            continue
        seen.add(mac)
        rate = (c.get("tx_rate") or 0) / 1000
        active = (c.get("tx_bytes-r") or 0) + (c.get("rx_bytes-r") or 0) > ACTIVE_BPS
        weak = (c.get("signal") or 0) < WEAK_DBM
        slow = active and 0 < rate < SLOW_MBPS
        poor = (weak or slow) and (c.get("uptime") or 0) >= MIN_UPTIME
        bad = poor and ap_air.get(ap, 0) >= CONGESTED_AIRTIME
        rec = clients.setdefault(mac, {"streak": 0, "kicks": []})
        rec["pstreak"] = rec.get("pstreak", 0) + 1 if poor else 0
        rec["episodes"] = [t for t in rec.get("episodes", []) if now - t < 3600]
        if rec["pstreak"] == STREAK_TO_KICK:
            rec["episodes"].append(now)
        rec["why"] = ("訊號弱" if weak else "") + ("、" if weak and slow else "") + ("速率低" if slow else "")
        if bad:
            per_ap[ap]["bad"] += 1
            rec["streak"] = rec["streak"] + 1 if rec.get("ap") == ap else 1
        else:
            rec["streak"] = 0
        rec.update(ap=ap, signal=c.get("signal"), rate=round(rate, 1), last=now)
        rec["kicks"] = [k for k in rec["kicks"] if now - k < 3600]
        if rec["streak"] >= STREAK_TO_KICK:
            reason = (f"訊號 {c.get('signal')} dBm" if weak else "") + \
                     (("、" if weak else "") + f"速率 {round(rate, 1)} Mbps" if slow else "")
            if rec["kicks"] and now - rec["kicks"][-1] < COOLDOWN:
                continue
            if len(rec["kicks"]) >= MAX_KICKS_PER_HOUR:
                rec["gave_up"] = True        # 踢過仍不佳,多半是死角或老舊裝置,只記錄
                continue
            candidates.append((rec["streak"], mac, ap, reason, c.get("signal"), rate))

    # 先處理最久的;每輪、每台 AP 都有上限
    candidates.sort(reverse=True)
    actions, used_ap = [], {}
    for streak, mac, ap, reason, sig, rate in candidates:
        if len(actions) >= MAX_PER_ROUND:
            break
        if used_ap.get(ap, 0) >= MAX_PER_AP:
            continue
        ok, err = True, ""
        if MODE == "kick":
            try:
                call("/cmd/stamgr", {"cmd": "kick-sta", "mac": mac})
                clients[mac]["kicks"].append(now)
                clients[mac]["streak"] = 0
            except Exception as e:
                ok, err = False, str(e)[:80]
        used_ap[ap] = used_ap.get(ap, 0) + 1
        actions.append({"ts": ts, "mode": MODE, "ap": ap_name.get(ap, "?"), "mac": mac,
                        "reason": reason, "minutes": streak, "signal": sig, "rate": round(rate, 1),
                        "ok": ok, "err": err})

    # 已離線超過一小時的裝置不再追蹤
    for mac in [m for m, r in clients.items() if m not in seen and now - r.get("last", 0) > 3600]:
        del clients[mac]

    persistent = [{"mac": m, "ap": ap_name.get(r.get("ap"), "?"), "minutes": r["pstreak"],
                   "signal": r.get("signal"), "rate": r.get("rate"), "why": r.get("why", "")}
                  for m, r in clients.items() if m in seen and r.get("pstreak", 0) >= STREAK_TO_KICK]
    persistent.sort(key=lambda x: -x["minutes"])
    by_ap = {}
    for x in persistent:
        by_ap[x["ap"]] = by_ap.get(x["ap"], 0) + 1

    repeat = [x for x in persistent
              if x["minutes"] >= LONG_MINUTES or len(clients[x["mac"]].get("episodes", [])) >= REPEAT_EPISODES]
    hot_aps = {k: v for k, v in by_ap.items() if v >= GROUP_AP}
    group = None
    if len(persistent) >= GROUP_SITE or hot_aps or len(repeat) >= GROUP_REPEAT:
        why = []
        if len(persistent) >= GROUP_SITE:
            why.append(f"全場 {len(persistent)} 台同時連續 3 分鐘以上連線不佳")
        if hot_aps:
            why.append("集中在 " + "、".join(f"AP {k}({v} 台)" for k, v in hot_aps.items()))
        if len(repeat) >= GROUP_REPEAT:
            why.append(f"{len(repeat)} 台是同一批裝置反覆或長時間不佳")
        weak_n = sum(1 for x in persistent if "訊號弱" in x["why"])
        slow_n = sum(1 for x in persistent if "速率低" in x["why"])
        group = {"why": why, "weak": weak_n, "slow": slow_n, "count": len(persistent),
                 "hot_aps": hot_aps, "repeat": len(repeat)}
        print("PERSIST_ALERT " + ";".join(why) + f";訊號弱 {weak_n} 台、速率低 {slow_n} 台")

    st["recent"] = (actions + st.get("recent", []))[:30]
    json.dump(st, open(STATE, "w"), ensure_ascii=False)

    # 本機逐分鐘紀錄(含完整 MAC,不進 repo)
    os.makedirs(LOGDIR, exist_ok=True)
    snap = {"ts": ts, "mode": MODE, "clients": len(sta),
            "aps": {ap_name[m]: {"air": ap_air[m], "clients": per_ap.get(m, {}).get("n", 0),
                                 "bad": per_ap.get(m, {}).get("bad", 0)} for m in ap_name},
            "watching": sum(1 for r in clients.values() if r["streak"] > 0),
            "persistent": persistent,
            "group": group,
            "actions": actions}
    with open(os.path.join(LOGDIR, ts[:10] + ".jsonl"), "a") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # 給儀表板的摘要(MAC 遮蔽)
    try:
        D = json.load(open(os.path.join(HERE, "data.json")))
        D["optimize"] = {
            "mode": MODE,
            "watching": snap["watching"],
            "gave_up": sum(1 for r in clients.values() if r.get("gave_up")),
            "recent": [dict(a, mac=mask(a["mac"])) for a in st["recent"][:12]],
            "persistent_count": len(persistent),
            "persistent_by_ap": dict(sorted(by_ap.items(), key=lambda x: -x[1])),
            "persistent": [dict(x, mac=mask(x["mac"])) for x in persistent[:20]],
        }
        if group:
            focus = "、".join(f"AP {k}" for k in hot_aps) or "多台 AP"
            D["issues"].append({
                "sev": "warn",
                "title": f"{group['count']} 台裝置持續連線不佳(連續 3 分鐘以上)",
                "detail": ";".join(group["why"]) + f"。其中訊號弱(< {WEAK_DBM} dBm){group['weak']} 台、"
                          f"使用中速率低(< {SLOW_MBPS} Mbps){group['slow']} 台。",
                "fix": f"先看 {focus} 的空中時間與他源干擾:訊號弱為主代表座位離 AP 太遠或被擋,"
                       f"可調高附近 AP 功率一級;速率低為主代表空中時間塞滿,可分流到鄰近較空的 AP。"
                       f"目前是觀察模式,不會自動踢除。",
                "key": "persistent-poor-group"})
            D["summary"]["warn"] = sum(1 for i in D["issues"] if i["sev"] == "warn")
        json.dump(D, open(os.path.join(HERE, "data.json"), "w"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    verb = "踢除" if MODE == "kick" else "會踢除(觀察模式)"
    print(f"optimize: 持續不佳(>= {STREAK_TO_KICK} 分鐘) {len(persistent)} 台 {by_ap},本輪{verb} {len(actions)} 台")


if __name__ == "__main__":
    main()
