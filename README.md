# YC 松菸 網路戰情室

活動現場 WiFi 與核心網路的即時監控儀表板，沿用 [manbao-wifi-dashboard](https://github.com/gaskhuang/manbao-wifi-dashboard) 的架構。

**儀表板：** https://gaskhuang.github.io/yc-wifi-dashboard/

| 設備 | 讀取方式 |
|---|---|
| UniFi U7 Pro × 8（HostiFi site `x9ajowek`） | UniFi OS API，API 金鑰 |
| MikroTik RB5009（雙 PPPoE 主備） | RouterOS REST（DDNS:8089），只允許公司 IP |
| Alta S24-POE / S8-POE | `alta_watch.js` 常駐 headless 瀏覽器登入 manage.alta.inc，每 20 秒把即時資料寫成 `alta.json`（Alta 無公開 API） |

憑證放在 `~/.config/zonetech/yc.env`（`YC_UNIFI_API_KEY`、`YC_NOC_USER`、`YC_NOC_PASS`），不進 repo。

```bash
screen -dmS yc bash -c 'cd ~/yc-wifi-dashboard; while true; do echo "=== $(date "+%F %T")" >> loop.log; ZONETECH_SECRETS=~/.config/zonetech/yc.env ./auto-update.sh >> loop.log 2>&1; echo "exit=$?" >> loop.log; sleep 180; done'
screen -S "$(screen -ls | grep -oE "[0-9]+\.yc\b")" -X quit   # 停止(-S yc 會同時對到 yc-alta)
```

Alta 收集器（另開一個 screen，需要 `npm install` 與本機 Playwright Chromium）：

```bash
screen -dmS yc-alta bash -c 'cd ~/yc-wifi-dashboard; set -a; . ~/.config/zonetech/yc.env; set +a; while true; do node alta_watch.js >> alta.log 2>&1; sleep 10; done'
```
