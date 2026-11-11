#!/usr/bin/env python3
"""每輪把儀表板資料(data.json)與 Alta 快照(alta.json)各追加一行到 logs/,供活動後分析;NAS 同步由 auto-update.sh 做"""
import json, os
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
STATE = os.path.join(LOGDIR, "archive_state.json")
TPE = timezone(timedelta(hours=8))


def append(name, rec):
    day = datetime.now(TPE).strftime("%Y-%m-%d")
    with open(os.path.join(LOGDIR, f"{name}-{day}.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    for name, path, key in (("dashboard", "data.json", "ts"), ("alta", "alta.json", "written")):
        try:
            d = json.load(open(os.path.join(HERE, path)))
        except (OSError, ValueError):
            continue
        # 同一份快照只記一次(Alta 斷線時 alta.json 不會更新)
        if d.get(key) and d.get(key) != state.get(name):
            d.pop("history", None)   # 儀表板的趨勢陣列每輪都重複,不存
            append(name, d)
            state[name] = d.get(key)
    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    main()
