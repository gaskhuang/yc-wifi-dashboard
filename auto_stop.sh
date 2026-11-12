#!/bin/bash
# 設備全部離線(AP 與交換器都 0 台在線)連續 STOP_AFTER 秒後,結束收集:
# 同步 NAS → 停掉儀表板迴圈與 Alta 收集器 → 寫結束紀錄 → 再同步一次 NAS
cd "$(dirname "$0")"
STOP_AFTER=${STOP_AFTER:-900}
NAS_ROOT="$HOME/nas-mounts/unifi-logs"
NAS_DIR="${YC_NAS_DIR:-$NAS_ROOT/events/2026-11-11_YC-songyan}"
log() { echo "$(date '+%F %T') $*" | tee -a logs/auto-stop.log; }
sync_nas() { mount | grep -q " on $NAS_ROOT " && mkdir -p "$NAS_DIR" && rsync -a logs/ "$NAS_DIR/" && log "NAS 同步完成" || log "NAS 同步失敗"; }

log "自動結束監看啟動(全部離線連續 ${STOP_AFTER}s 才結束)"
since=""
while true; do
  # 只看 3 分鐘內的新資料;資料本身過期(迴圈停了)不算離線
  state=$(python3 - <<'EOF'
import json, time
from datetime import datetime
try:
    d = json.load(open("data.json"))
except Exception:
    print("unknown"); raise SystemExit
age = time.time() - datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
s = d["summary"]
if age > 420:
    print("stale")
elif s.get("online", 0) == 0 and s.get("switches_online", 0) == 0:
    print("all_offline")
else:
    print(f"online ap={s.get('online')} sw={s.get('switches_online')}")
EOF
)
  if [ "$state" = "all_offline" ]; then
    [ -z "$since" ] && since=$(date +%s) && log "偵測到全部離線,開始計時"
    if [ $(( $(date +%s) - since )) -ge "$STOP_AFTER" ]; then
      log "全部離線已連續 ${STOP_AFTER}s,結束收集"
      sync_nas
      # screen -X quit 不會結束子程式,直接找出迴圈與子程式結束
      pkill -f "while true; do echo \"=== " ; pkill -f "auto-update.sh"
      pkill -f "node alta_watch.js >> alta.log" ; pkill -f "node alta_watch.js"
      for s in yc yc-alta; do id=$(screen -ls | grep -oE "[0-9]+\.$s\b"); [ -n "$id" ] && screen -S "$id" -X quit; done
      echo "收集結束:$(date '+%F %T')(AP 與交換器全部離線連續 ${STOP_AFTER}s)" > logs/collection-ended.txt
      sync_nas
      log "已停止儀表板與 Alta 收集器"
      exit 0
    fi
  else
    [ -n "$since" ] && log "有設備恢復在線($state),取消計時"
    since=""
  fi
  sleep 60
done
