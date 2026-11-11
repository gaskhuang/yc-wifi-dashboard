#!/usr/bin/env python3
"""讀 data.json,把「狀態有變化」的告警彙整成一則 LINE 訊息推播

規則(避免活動當天手機被洗版,也省 LINE 免費方案的則數):
  - 只推嚴重與警告,提示類不推
  - 告警要連續出現 N 輪才推(嚴重 2 輪、警告 5 輪),短暫抖動不會推
  - 已推過的告警連續消失 2 輪才算恢復,恢復也會通知
  - 嚴重告警沒解決時,每 60 分鐘提醒一次
  - 同一輪的所有變化合併成一則;每小時最多推 4 則,超過的留到下一輪
  - 第一次執行只推一則「監控啟動」摘要,並把當下的告警當成已知狀態

憑證從環境變數讀:LINE_ALERT_TOKEN(Messaging API channel access token)、
LINE_ALERT_TO(要推給誰的 userId 或 groupId)。沒設定時只印出訊息,不推也不記狀態。

用法:
  python3 notify.py            # 正常執行
  python3 notify.py --dry-run  # 只印出這一輪會推什麼,不推、不記狀態
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "notify_state.json")

HOLD = {"crit": 2, "warn": 5}     # 連續出現幾輪才推
RECOVER_AFTER = 2                 # 連續消失幾輪才算恢復
REMIND_MIN = 60                   # 嚴重告警未解決的提醒間隔(分鐘)
MAX_PER_HOUR = 4                  # 每小時最多推幾則
MAX_ITEMS = 8                     # 每個段落最多列幾項

TOKEN = os.environ.get("LINE_ALERT_TOKEN", "")
TO = os.environ.get("LINE_ALERT_TO", "")
DASH_URL = os.environ.get("DASHBOARD_URL", "")


def push(text):
    body = json.dumps({"to": TO, "messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request("https://api.line.me/v2/bot/message/push", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.read()[:200].decode(errors='replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def section(label, items):
    if not items:
        return []
    lines = [f"{label}({len(items)})"]
    lines += [f"・{t}" for t in items[:MAX_ITEMS]]
    if len(items) > MAX_ITEMS:
        lines.append(f"・…另有 {len(items) - MAX_ITEMS} 項")
    return lines


def main():
    dry = "--dry-run" in sys.argv or not (TOKEN and TO)
    D = json.load(open(os.path.join(HERE, "data.json")))
    now = time.time()
    s = D["summary"]
    current = {i["key"]: i for i in D["issues"] if i["sev"] in HOLD}

    first_run = not os.path.exists(STATE)
    st = {"issues": {}, "sent": []} if first_run else json.load(open(STATE))
    known = st["issues"]

    new_crit, new_warn, escalated, recovered, remind = [], [], [], [], []

    # 目前出現的告警
    for k, i in current.items():
        rec = known.get(k)
        if rec is None:
            rec = known[k] = {"sev": i["sev"], "title": i["title"], "count": 0,
                              "first": now, "notified": False, "notified_at": 0, "missing": 0}
        prev_sev = rec["sev"]
        rec.update(count=rec["count"] + 1, missing=0, title=i["title"], sev=i["sev"])
        if first_run:
            rec.update(notified=True, notified_at=now)    # 啟動時的狀態視為已知
            continue
        if not rec["notified"]:
            if rec["count"] >= HOLD[i["sev"]]:
                (new_crit if i["sev"] == "crit" else new_warn).append(k)
        elif i["sev"] == "crit" and prev_sev != "crit":
            escalated.append(k)                            # 警告升級為嚴重
        elif i["sev"] == "crit" and now - rec["notified_at"] >= REMIND_MIN * 60:
            remind.append(k)

    # 這一輪消失的告警
    for k in list(known):
        if k in current:
            continue
        rec = known[k]
        rec["missing"] += 1
        if rec["missing"] >= RECOVER_AFTER:
            if rec["notified"] and not first_run:
                recovered.append(k)
            else:
                del known[k]                               # 沒推過就消失:當作沒發生

    stamp = datetime.now().strftime("%m/%d %H:%M")
    head = f"【{D['site']} 網路戰情室】{stamp}"
    status = (f"目前:線上 AP {s['online']}/{s['online'] + s['offline']}・用戶 {s['clients']}"
              f"・嚴重 {s['crit']}・警告 {s['warn']}")

    if first_run:
        lines = [head, "監控已啟動。", status]
        if s["crit"] or s["warn"]:
            lines += section("目前的問題", [i["title"] for i in current.values()])
    else:
        mins = lambda k: int((now - known[k]["first"]) / 60)
        lines = [head]
        lines += section("🔴 新的嚴重問題", [known[k]["title"] for k in new_crit])
        lines += section("🔴 警告升級為嚴重", [known[k]["title"] for k in escalated])
        lines += section("🟡 新的警告", [known[k]["title"] for k in new_warn])
        lines += section("✅ 已恢復", [known[k]["title"] for k in recovered])
        lines += section("⏰ 仍未解決", [f"{known[k]['title']}(已持續 {mins(k)} 分鐘)" for k in remind])
        if len(lines) == 1:
            if not dry:
                json.dump(st, open(STATE, "w"), ensure_ascii=False)
            print("notify: 沒有需要推播的變化")
            return
        lines.append(status)
    if DASH_URL:
        lines.append(f"儀表板 {DASH_URL}")
    text = "\n".join(lines)

    if dry:
        print("notify: [僅預覽,未推播" + ("" if TOKEN and TO else ",LINE_ALERT_TOKEN / LINE_ALERT_TO 尚未設定") + "]")
        print(text)
        return

    # 每小時上限:超過就不推、不記為已通知,下一輪會再試
    st["sent"] = [t for t in st.get("sent", []) if now - t < 3600]
    if len(st["sent"]) >= MAX_PER_HOUR and not first_run:
        json.dump(st, open(STATE, "w"), ensure_ascii=False)
        print(f"notify: 本小時已推 {len(st['sent'])} 則,這一輪延後")
        return

    ok, err = push(text)
    if not ok:
        # 推失敗就不更新通知狀態,下一輪重試
        for k in recovered:
            known[k]["missing"] = 0
        json.dump(st, open(STATE, "w"), ensure_ascii=False)
        print("notify: LINE 推播失敗", err, file=sys.stderr)
        sys.exit(1)

    st["sent"].append(now)
    for k in new_crit + new_warn + escalated + remind:
        known[k].update(notified=True, notified_at=now)
    for k in recovered:
        del known[k]
    json.dump(st, open(STATE, "w"), ensure_ascii=False)
    print("notify: 已推播\n" + text)


if __name__ == "__main__":
    main()
