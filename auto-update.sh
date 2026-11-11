#!/bin/bash
set -e

cd "$(dirname "$0")"

# 憑證一律從本機密鑰檔載入，不進 repo
set -a; . "${ZONETECH_SECRETS:-$HOME/.config/zonetech/secrets.env}"; set +a
# LINE 推播訊息附上的儀表板連結
export DASHBOARD_URL="https://gaskhuang.github.io/yc-wifi-dashboard/"
python3 fetch_stats.py
# 活動紀錄收集（UniFi 事件、RB5009 紀錄、用戶快照、每小時匯出報表），存在本機 logs/，不進 repo
python3 collect_logs.py || true
# 連線優化：塞住的 AP 上持續不佳的裝置踢掉重連（預設觀察模式，OPTIMIZE_MODE=kick 才會真的踢）
python3 optimize_clients.py || true
python3 render.py
# LINE 推播失敗不影響網頁更新
python3 notify.py || true

# 推送到 GitHub（尚未建立 repo 時略過）
# 每分鐘推一次；網頁由 GitHub Actions 發布（.github/workflows/pages.yml）
git rev-parse --git-dir >/dev/null 2>&1 || exit 0
PUSH_EVERY=${PUSH_EVERY:-55}
last=$(cat .last_push 2>/dev/null || echo 0)
[ $(( $(date +%s) - last )) -lt "$PUSH_EVERY" ] && exit 0
if git diff --quiet dashboard.html index.html data.json 2>/dev/null; then
  exit 0
fi

# 先同步遠端（程式碼更新會在下一輪生效），衝突就放棄這次同步
git pull -q --rebase --autostash origin main || git rebase --abort 2>/dev/null || true
git add dashboard.html index.html data.json
STATS=$(python3 -c "import json; d=json.load(open('data.json')); s=d['summary']; print(f\"{d['ts']} · {s['online']} AP · {s['clients']} 用戶 · {s['crit']} 嚴重 · {s['warn']} 警告\")")
git commit -m "Auto-update: $STATS"
git push origin main
date +%s > .last_push
