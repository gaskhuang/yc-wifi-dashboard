#!/usr/bin/env python3
"""讀 data.json,產生單檔 HTML 儀表板"""
import base64, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "data.json")))

SEV = {
    "crit": ("嚴重", "var(--crit)"),
    "warn": ("警告", "var(--warn)"),
    "info": ("提示", "var(--info)"),
}

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def uptime_fmt(sec):
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d: return f"{d}天{h}時"
    if h: return f"{h}時{m}分"
    return f"{m}分"

def level(v, warn, crit):
    return "crit" if v >= crit else "warn" if v >= warn else "ok"

def meter(label, value, unit, warn, crit, note=""):
    lv = level(value, warn, crit)
    pct = max(0, min(100, value))
    return f"""<div class="m">
      <div class="m-top"><span class="m-lab">{esc(label)}</span>
        <span class="m-val {lv}">{value:g}<span class="m-u">{esc(unit)}</span></span></div>
      <div class="track"><i class="fill {lv}" style="width:{pct}%"></i>
        <u class="tick" style="left:{warn}%"></u><u class="tick" style="left:{crit}%"></u></div>
      {f'<div class="m-note">{esc(note)}</div>' if note else ''}
    </div>"""

def spark(vals, w=132, h=30):
    if len(vals) < 3:
        return '<div class="spark-empty">趨勢資料累積中</div>'
    lo, hi = 0, max(max(vals), 10)
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i*step:.1f},{h - (v-lo)/(hi-lo)*h:.1f}" for i, v in enumerate(vals))
    area = f"0,{h} " + pts + f" {w},{h}"
    lastx, lasty = (len(vals)-1)*step, h - (vals[-1]-lo)/(hi-lo)*h
    return f"""<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">
      <polygon points="{area}" class="sp-area"/>
      <polyline points="{pts}" class="sp-line"/>
      <circle cx="{lastx:.1f}" cy="{lasty:.1f}" r="2.4" class="sp-dot"/></svg>"""

# ---------- AP 卡片 ----------
hist = D.get("history", [])
cards = []
for a in D["aps"]:
    air_hist = [h["ap"][a["name"]]["air"] for h in hist if a["name"] in h.get("ap", {})]
    worst = max(level(a["airtime"], 50, 70),
                level(a["interference"], 25, 40),
                level(a["tx_retry"], 20, 30) if a["retry_ok"] else "ok",
                key=lambda x: {"ok": 0, "warn": 1, "crit": 2}[x])
    retry_html = (meter("TX 重試", round(a["tx_retry"], 1), "%", 20, 30)
                  if a["retry_ok"] else
                  '<div class="m"><div class="m-top"><span class="m-lab">TX 重試</span>'
                  '<span class="m-val na">—</span></div>'
                  '<div class="m-note">計數器重置中,樣本不足</div></div>')
    FULL_FACTS = f"""<dl class="facts">
        <div><dt>發射功率</dt><dd>{a['tx_power']} dBm <span class="sub">{esc(a['tx_power_mode'])}</span></dd></div>
        <div><dt>CPU</dt><dd class="{level(a['cpu'],60,80)}">{a['cpu']:.0f}%</dd></div>
        <div><dt>記憶體</dt><dd class="{level(a['mem'],85,90)}">{a['mem']:.0f}%</dd></div>
        <div><dt>運作時間</dt><dd>{uptime_fmt(a['uptime'])}</dd></div>
        <div><dt>IP</dt><dd class="mono">{esc(a['ip'])}</dd></div>
        <div><dt>Min RSSI</dt><dd>{(str(a['min_rssi']) + ' dBm') if a['min_rssi_on'] else '未啟用'}</dd></div>
      </dl>"""
    if a.get("virtual"):
        # 雙 5GHz 機種的第二個無線電:整機數據已顯示在主卡片,這裡只放無線電本身的
        facts = f"""<dl class="facts">
        <div><dt>發射功率</dt><dd>{a['tx_power']} dBm <span class="sub">{esc(a['tx_power_mode'])}</span></dd></div>
        <div><dt>Min RSSI</dt><dd>{'已啟用' if a['min_rssi_on'] else '未啟用'}</dd></div>
        <div><dt>所屬設備</dt><dd>{esc(a.get('device', ''))}(第二個 5GHz 無線電)</dd></div>
      </dl>"""
    else:
        facts = None
    cards.append(f"""<article class="ap {worst}">
      <header class="ap-h">
        <div class="ap-id">AP <b>{esc(a.get('device') if a.get('virtual') else a['name'])}</b>{'<span class="ap-sub">第二無線電</span>' if a.get('virtual') else ''}</div>
        <div class="ap-tags">
          <span class="tag{' dfs' if a['dfs'] else ''}">ch {a['channel']} · {a['bw']}MHz</span>
          <span class="tag">{a['clients']} 人</span>
        </div>
      </header>
      <div class="meters">
        {meter("總空中時間", a["airtime"], "%", 50, 70, f"自身 {a['self_use']}% · 他源 {a['interference']}%")}
        {meter("他源干擾", a["interference"], "%", 25, 40)}
        {retry_html}
      </div>
      <div class="sparkwrap"><span class="sp-lab">空中時間趨勢</span>{spark(air_hist)}</div>
      {facts or FULL_FACTS}
    </article>""")

# ---------- 問題清單 ----------
if D["issues"]:
    items = "".join(f"""<li class="iss {i['sev']}">
        <div class="iss-h"><span class="pill {i['sev']}">{SEV[i['sev']][0]}</span>
          <h3>{esc(i['title'])}</h3></div>
        <p class="iss-d">{esc(i['detail'])}</p>
        <p class="iss-f"><span>建議</span>{esc(i['fix'])}</p></li>""" for i in D["issues"])
    issues_html = f'<ol class="isslist">{items}</ol>'
else:
    issues_html = '<p class="allclear">目前所有指標都在正常範圍,沒有需要處理的項目。</p>'

# ---------- MikroTik 面板 ----------
mt = D.get("mikrotik")
if not mt and not D.get("mt_configured"):
    mt_html = ('<div class="mtbad">MikroTik 尚未設定(等待 DDNS),DHCP 與對外頻寬資料暫不顯示</div>')
elif not mt:
    mt_html = ('<div class="mtbad">RB5009 REST API 無回應,DHCP 與對外頻寬資料本輪無法取得</div>')
else:
    def rate(v):
        return f'{v:g}' if v is not None else '—'
    pu = mt.get("pool_used_pct")
    pool_lv = "crit" if (pu or 0) >= 80 else "warn" if (pu or 0) >= 60 else "ok"
    ppp_ok = mt.get("wan_running")
    psu = mt.get("psu_fail") or []
    mt_html = f"""<div class="mtgrid">
      <div class="mtcard">
        <span class="mtlab">對外連線</span>
        <b class="mtval {'ok' if ppp_ok else 'crit'}">{'連線中' if ppp_ok else '中斷'}</b>
        <span class="mtsub mono">{' · '.join(f"{esc(w['name'])}{'(使用中)' if w['active'] else ''} {'✓' if w['running'] else '✗'}" for w in mt.get('wans') or []) or esc(mt.get('wan_name') or '—')}</span>
      </div>
      <div class="mtcard">
        <span class="mtlab">下載(即時)</span>
        <b class="mtval">{rate(mt.get('down_mbps'))}<i>Mbps</i></b>
        <span class="mtsub">上傳 {rate(mt.get('up_mbps'))} Mbps</span>
      </div>
      <div class="mtcard">
        <span class="mtlab">DHCP 已配發</span>
        <b class="mtval {pool_lv}">{mt.get('leases_bound')}<i>/{mt.get('pool_capacity')}</i></b>
        <span class="mtsub">用量 {pu if pu is not None else '—'}%</span>
      </div>
      <div class="mtcard">
        <span class="mtlab">其中用戶端裝置</span>
        <b class="mtval">{mt.get('clients')}</b>
        <span class="mtsub">另有 {mt.get('infra')} 台網路設備</span>
      </div>
      <div class="mtcard">
        <span class="mtlab">RB5009 狀態</span>
        <b class="mtval {'warn' if psu else 'ok'}">{'電源異常' if psu else '正常'}</b>
        <span class="mtsub">CPU {mt.get('cpu', 0)}% · {mt.get('cpu_temp', 0)}°C{(' · ' + '、'.join(x.upper() for x in psu) + ' 異常') if psu else ''}</span>
      </div>
      <div class="mtcard wide">
        <span class="mtlab">位址池範圍</span>
        <b class="mtval sm mono">{esc(mt.get('pool_range'))}</b>
        <span class="mtsub">共 {mt.get('pool_capacity')} 個可用位址 · RouterOS {esc(mt.get('version') or '')}</span>
      </div>
    </div>"""

# ---------- CRS418 核心交換器 ----------
crs = D.get("crs")
if crs:
    psu = crs.get("psu_fail") or []
    rows = []
    for pt in crs["ports"]:
        if not pt["up"] and not pt["neighbor"] and pt["poe_status"] in (None, "waiting-for-load", "disabled"):
            continue                       # 沒接東西的埠不列
        sp = pt["rate_mbps"] or 0
        link = (f'<td class="n {"r" if 0 < sp < 1000 else "g"}">{esc(pt["rate"])}</td>' if pt["up"]
                else '<td class="n">未連線</td>')
        if pt["poe_w"]:
            poe = f'{pt["poe_w"]:g} W'
        elif pt["poe_status"] and pt["poe_status"] not in ("waiting-for-load", "disabled", "powered-on"):
            poe = f'<span style="color:var(--crit)">{esc(pt["poe_status"])}</span>'
        else:
            poe = "—"
        rows.append(f'<tr><td class="n">{esc(pt["name"])}</td><td>{esc(pt["neighbor"] or "")}</td>{link}<td class="n">{poe}</td></tr>')
    status = (f'<span class="swst crit">電源異常:{"、".join(x.upper() for x in psu)}</span>' if psu
              else '<span class="swst ok">線上</span>')
    crs_card = f"""<div class="swcard">
      <div class="swhead"><b>CRS418 核心交換器</b>{status}</div>
      <div class="swmeta mono">{esc(crs.get('board') or '')} · RouterOS {esc(crs.get('version') or '')} · {crs.get('temp', 0)}°C</div>
      <div class="swpoe"><span>PoE 用量</span><b>{crs.get('poe_total_w', 0):g} W</b><span>CPU {crs.get('cpu', 0)}%</span></div>
      <table class="ref swtbl"><thead><tr><th>埠</th><th>接的設備</th><th>連線</th><th>PoE</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="4">沒有連線中的埠</td></tr>'}</tbody></table>
    </div>"""
elif D.get("crs_configured", D.get("mt_configured")):
    crs_card = '<div class="swcard"><div class="swhead"><b>CRS418 核心交換器</b><span class="swst crit">無法連線</span></div></div>'
else:
    crs_card = ""

# ---------- 交換器 / PoE 面板 ----------
sw_parts = []
for sw in D.get("switches", []):
    if sw["online"]:
        pct = sw.get("poe_pct") or 0
        lv = "crit" if pct >= 90 else "warn" if pct >= 80 else "ok"
        status = '<span class="swst ok">線上</span>'
        budget = (f"{sw['poe_used']:g} / {'約 ' if sw['poe_budget_est'] else ''}{sw['poe_budget']:g} W"
                  if sw.get("poe_budget") else f"{sw['poe_used']:g} W")
        poe_line = f'<div class="swpoe"><span>PoE 用量</span><b class="{lv}">{budget}</b><span>{pct:g}%</span></div>'
    else:
        where = "現場" if sw["at_venue"] else "公司網段"
        status = f'<span class="swst {"crit" if sw["at_venue"] else "info"}">離線・最後在{where}</span>'
        poe_line = '<div class="swpoe"><span>PoE 用量</span><b>—</b><span>離線時無資料</span></div>'
    rows = []
    for pt in sw["ports"]:
        if pt["up"] is None:
            link = '<td class="n">—</td>'
        elif pt["up"]:
            sp = pt["speed"] or 0
            cls = "r" if 0 < sp < 1000 else "g"
            link = f'<td class="n {cls}">{sp:g} Mbps</td>' if sp else '<td class="n g">連線</td>'
        else:
            link = '<td class="n">未連線</td>'
        poe = "—" if pt["poe_w"] is None else f"{pt['poe_w']:g} W"
        dev = f"AP {esc(pt['ap'])}" if pt["ap"] else ("上行／供電輸入" if not pt["poe"] else "")
        rows.append(f'<tr><td class="n">{pt["idx"]}</td><td>{dev}</td>{link}<td class="n">{poe}</td></tr>')
    sw_parts.append(f"""<div class="swcard">
      <div class="swhead"><b>{esc(sw['name'])}</b>{status}</div>
      <div class="swmeta mono">{esc(sw.get('model') or '')} · {esc(sw.get('ip') or '—')}</div>
      {poe_line}
      <table class="ref swtbl"><thead><tr><th>埠</th><th>接的設備</th><th>連線</th><th>PoE</th></tr></thead>
      <tbody>{''.join(rows)}</tbody></table>
    </div>""")
sw_parts = ([crs_card] if crs_card else []) + sw_parts
sw_html = (f'<div class="swgrid">{"".join(sw_parts)}</div>' if sw_parts
           else '<p class="allclear">此站點沒有交換器資料。</p>')

# ---------- 連線優化 ----------
op = D.get("optimize")
if op:
    mode_txt = "自動踢除中" if op.get("mode") == "kick" else "觀察模式(只記錄,不踢)"
    rows = "".join(
        f'<tr><td class="n">{esc(a["ts"][11:16])}</td><td>AP {esc(a["ap"])}</td><td class="n">{esc(a["mac"])}</td>'
        f'<td>{esc(a["reason"])}</td><td class="n">{a["minutes"]} 分鐘</td>'
        f'<td>{"已踢除" if a["mode"] == "kick" and a["ok"] else ("失敗" if a["mode"] == "kick" else "會踢除")}</td></tr>'
        for a in op.get("recent", []))
    opt_html = f"""<div class="swpoe"><span>模式</span><b>{mode_txt}</b>
      <span>連續 3 分鐘以上連線不佳 {op.get("persistent_count", 0)} 台{(" · " + "、".join(f"AP {k} {v} 台" for k, v in (op.get("persistent_by_ap") or {}).items())) if op.get("persistent_by_ap") else ""}</span></div>
      <div class="scroll"><table class="ref"><thead><tr><th>時間</th><th>AP</th><th>裝置</th><th>原因</th><th>持續</th><th>動作</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="6">目前沒有需要重新連線的裝置</td></tr>'}</tbody></table></div>"""
else:
    opt_html = '<p class="allclear">連線優化尚未啟用。</p>'

s = D["summary"]
if s["crit"]:
    verdict, vclass = f"{s['crit']} 項需要立即處理", "crit"
elif s["warn"]:
    verdict, vclass = f"{s['warn']} 項需要留意", "warn"
else:
    verdict, vclass = "全部指標正常", "ok"

clients_hist = [h["clients"] for h in hist]

CSS = """
:root{
  --bg:#F5F6F8; --panel:#FFFFFF; --line:#E1E4EA; --line2:#EDEFF3;
  --ink:#1B1F27; --ink2:#5A6273; --ink3:#8B93A3;
  --accent:#4B4FA6;
  --ok:#2E8B62; --warn:#C07A16; --crit:#C0392F; --info:#4B6EA9;
  --okbg:#E8F3ED; --warnbg:#FBF1DE; --critbg:#FBEAE8;
  --track:#E7EAEF;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#101319; --panel:#171B23; --line:#262C37; --line2:#1F242D;
    --ink:#E7EAF0; --ink2:#A2AAB9; --ink3:#6D7686;
    --accent:#8E93E8;
    --ok:#4FBE8B; --warn:#E0A63F; --crit:#E86B60; --info:#7CA0DC;
    --okbg:#16281F; --warnbg:#2C2415; --critbg:#301B19;
    --track:#242A34;
  }
}
:root[data-theme="dark"]{
  --bg:#101319; --panel:#171B23; --line:#262C37; --line2:#1F242D;
  --ink:#E7EAF0; --ink2:#A2AAB9; --ink3:#6D7686;
  --accent:#8E93E8;
  --ok:#4FBE8B; --warn:#E0A63F; --crit:#E86B60; --info:#7CA0DC;
  --okbg:#16281F; --warnbg:#2C2415; --critbg:#301B19;
  --track:#242A34;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans","Noto Sans TC",-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
.mono,.m-val,.facts dd,.spark-empty{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  font-variant-numeric:tabular-nums}

/* header */
.top{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;
  padding-bottom:16px;border-bottom:1px solid var(--line)}
.top h1{margin:0;font-size:21px;font-weight:600;letter-spacing:-.01em;display:flex;align-items:center}
.top h1 .logo{height:40px;width:auto;display:block;border-radius:3px}
.top .site{color:var(--ink3);font-size:13px;margin-left:10px;font-weight:400}
.stamp{font-size:12.5px;color:var(--ink2);font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.stamp b{color:var(--ink);font-weight:500}

/* verdict */
.verdict{display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin:20px 0 26px;
  padding:15px 18px;border:1px solid var(--line);border-left:4px solid var(--ink3);
  border-radius:4px;background:var(--panel)}
.verdict.ok{border-left-color:var(--ok);background:var(--okbg)}
.verdict.warn{border-left-color:var(--warn);background:var(--warnbg)}
.verdict.crit{border-left-color:var(--crit);background:var(--critbg)}
.verdict .vtxt{font-size:16.5px;font-weight:600}
.verdict.ok .vtxt{color:var(--ok)} .verdict.warn .vtxt{color:var(--warn)} .verdict.crit .vtxt{color:var(--crit)}
.kpis{display:flex;gap:22px;margin-left:auto;flex-wrap:wrap}
.kpi{text-align:right}
.kpi b{display:block;font-size:19px;font-weight:600;line-height:1.2;
  font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.kpi span{font-size:11px;color:var(--ink2);letter-spacing:.06em}

h2{font-size:12px;font-weight:600;letter-spacing:.1em;color:var(--ink3);
  text-transform:uppercase;margin:34px 0 14px}

/* mikrotik */
.mtgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px}
.mtcard{background:var(--panel);border:1px solid var(--line);border-radius:4px;
  padding:12px 14px;display:flex;flex-direction:column;gap:2px}
.mtcard.wide{grid-column:1/-1}
.mtlab{font-size:11.5px;color:var(--ink3);letter-spacing:.03em}
.mtval{font-size:22px;font-weight:600;line-height:1.25;
  font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.mtval.sm{font-size:14px}
.mtval i{font-size:12px;font-style:normal;font-weight:400;color:var(--ink3);margin-left:3px}
.mtval.ok{color:var(--ok)} .mtval.warn{color:var(--warn)} .mtval.crit{color:var(--crit)}
.mtsub{font-size:11.5px;color:var(--ink3);
  font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.mtbad{background:var(--critbg);border:1px solid var(--line);border-left:3px solid var(--crit);
  border-radius:4px;padding:13px 16px;color:var(--crit);font-weight:500}

/* issues */
.isslist{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.iss{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--ink3);
  border-radius:4px;padding:13px 16px}
.iss.crit{border-left-color:var(--crit)} .iss.warn{border-left-color:var(--warn)}
.iss.info{border-left-color:var(--info)}
.iss-h{display:flex;align-items:center;gap:10px}
.iss-h h3{margin:0;font-size:15px;font-weight:600}
.pill{font-size:10.5px;font-weight:600;letter-spacing:.08em;padding:2px 7px;border-radius:3px;flex:none}
.pill.crit{background:var(--critbg);color:var(--crit)}
.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.info{background:var(--okbg);color:var(--info)}
.iss-d{margin:7px 0 0;color:var(--ink2);font-size:14px}
.iss-f{margin:7px 0 0;font-size:14px;color:var(--ink)}
.iss-f span{font-size:10.5px;font-weight:600;letter-spacing:.08em;color:var(--accent);margin-right:8px}
.allclear{background:var(--okbg);border:1px solid var(--line);border-left:3px solid var(--ok);
  border-radius:4px;padding:14px 16px;margin:0;color:var(--ok);font-weight:500}

/* ap grid */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}
.ap{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--ok);
  border-radius:4px;padding:14px 16px 12px}
.ap.warn{border-top-color:var(--warn)} .ap.crit{border-top-color:var(--crit)}
.ap-h{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.ap-id{font-size:12px;color:var(--ink3);letter-spacing:.06em}
.ap-id b{font-size:19px;color:var(--ink);letter-spacing:0;
  font-family:"IBM Plex Mono",monospace;margin-left:3px}
.ap-tags{display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:11px;color:var(--ink2);background:var(--line2);border:1px solid var(--line);
  padding:2px 7px;border-radius:3px;font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.tag.dfs{color:var(--warn);border-color:var(--warn)}

.meters{display:grid;gap:9px}
.m-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.m-lab{font-size:12.5px;color:var(--ink2)}
.m-val{font-size:14px;font-weight:600}
.m-val.ok{color:var(--ink)} .m-val.warn{color:var(--warn)} .m-val.crit{color:var(--crit)}
.m-val.na{color:var(--ink3)}
.m-u{font-size:11px;margin-left:1px;font-weight:400;color:var(--ink3)}
.track{position:relative;height:5px;background:var(--track);border-radius:3px;margin-top:4px;overflow:hidden}
.fill{position:absolute;inset:0 auto 0 0;border-radius:3px;background:var(--ok)}
.fill.warn{background:var(--warn)} .fill.crit{background:var(--crit)}
.tick{position:absolute;top:0;bottom:0;width:1px;background:var(--panel);opacity:.85}
.m-note{font-size:11px;color:var(--ink3);margin-top:3px;
  font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}

.sparkwrap{display:flex;align-items:center;gap:10px;margin:12px 0 2px;
  padding-top:11px;border-top:1px solid var(--line2)}
.sp-lab{font-size:11px;color:var(--ink3);flex:none}
.spark{width:100%;height:30px;overflow:visible}
.sp-area{fill:var(--accent);opacity:.12}
.sp-line{fill:none;stroke:var(--accent);stroke-width:1.5;vector-effect:non-scaling-stroke;
  stroke-linejoin:round}
.sp-dot{fill:var(--accent)}
.spark-empty{font-size:11px;color:var(--ink3)}

.facts{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 14px;margin:11px 0 0;
  padding-top:11px;border-top:1px solid var(--line2)}
.facts div{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.facts dt{font-size:12px;color:var(--ink3)}
.facts dd{margin:0;font-size:12.5px;font-weight:500}
.facts dd.warn{color:var(--warn)} .facts dd.crit{color:var(--crit)} .facts dd.ok{color:var(--ink)}
.facts .sub{color:var(--ink3);font-weight:400;font-size:11px}

/* reference */
.ref{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--panel);
  border:1px solid var(--line);border-radius:4px;overflow:hidden}
.ref th,.ref td{padding:9px 13px;text-align:left;border-bottom:1px solid var(--line2)}
.ref thead th{font-size:11px;letter-spacing:.07em;color:var(--ink3);font-weight:600;
  background:var(--line2);text-transform:uppercase}
.ref tbody tr:last-child td{border-bottom:none}
.ref td.n{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;white-space:nowrap}
.ref .g{color:var(--ok)} .ref .a{color:var(--warn)} .ref .r{color:var(--crit)}
.scroll{overflow-x:auto}
.ap-sub{font-size:11px;color:var(--ink3);margin-left:8px;font-weight:400}
/* switch / poe */
.swgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.swcard{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:14px 14px 6px}
.swhead{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:14.5px}
.swst{font-size:11.5px;padding:2px 8px;border-radius:99px;white-space:nowrap}
.swst.ok{background:var(--okbg);color:var(--ok)} .swst.crit{background:var(--critbg);color:var(--crit)}
.swst.info{background:var(--track);color:var(--info)}
.swmeta{font-size:11.5px;color:var(--ink3);margin:4px 0 10px}
.swpoe{display:flex;align-items:baseline;gap:10px;font-size:12px;color:var(--ink3);margin-bottom:8px}
.swpoe b{font-size:18px;color:var(--ink);font-variant-numeric:tabular-nums}
.swpoe b.warn{color:var(--warn)} .swpoe b.crit{color:var(--crit)} .swpoe b.ok{color:var(--ok)}
.swtbl{font-size:12.5px;background:transparent}
.swtbl th,.swtbl td{padding:6px 8px}


.foot{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--ink3);display:grid;gap:5px}
.foot b{color:var(--ink2);font-weight:500}
@media (max-width:560px){.kpis{margin-left:0}.wrap{padding:20px 14px 48px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

SSID_TXT = (" · SSID " + " / ".join(esc(x) for x in D.get("ssids", []))) if D.get("ssids") else ""

# 左上角 logo:內嵌成 data URI,Pages 只發布 HTML/JSON 也能顯示
LOGO_B64 = base64.b64encode(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png"), "rb").read()).decode()
HTML = f"""<title>{esc(D['site'])}網路戰情室</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&family=Noto+Sans+TC:wght@400;500;700&display=swap">
<style>{CSS}</style>
<div class="wrap">
  <div class="top">
    <h1><img class="logo" src="data:image/png;base64,{LOGO_B64}" alt="Y Combinator"><span class="site">{esc(D['site'])}{SSID_TXT}</span></h1>
    <div class="stamp">資料時間 <b>{esc(D['ts'])}</b> · 每分鐘更新 · <span id="live">自動重新整理</span></div>
  </div>

  <div class="verdict {vclass}">
    <span class="vtxt">{esc(verdict)}</span>
    <div class="kpis">
      <div class="kpi"><b>{s['online']}</b><span>線上 AP</span></div>
      <div class="kpi"><b>{s['clients']}</b><span>連線用戶</span></div>
      <div class="kpi"><b class="{'crit' if s['crit'] else ''}" style="color:{'var(--crit)' if s['crit'] else 'inherit'}">{s['crit']}</b><span>嚴重</span></div>
      <div class="kpi"><b style="color:{'var(--warn)' if s['warn'] else 'inherit'}">{s['warn']}</b><span>警告</span></div>
      <div class="kpi"><b>{s.get('switches_online', 0)}<i style="font-size:.6em;font-style:normal;opacity:.6">/{s.get('switches', 0)}</i></b><span>交換器</span></div>
      <div class="kpi"><b>{s['spares']}</b><span>備用機</span></div>
    </div>
  </div>

  <h2>核心路由 MikroTik RB5009</h2>
  {mt_html}

  <h2>交換器／PoE 供電</h2>
  {sw_html}

  <h2>各台 AP 即時指標</h2>
  <div class="grid">{''.join(cards)}</div>

  <h2>連線優化</h2>
  {opt_html}

  <h2>需要調整的項目</h2>
  {issues_html}

  <h2>判讀基準</h2>
  <div class="scroll"><table class="ref">
    <thead><tr><th>指標</th><th>健康</th><th>注意</th><th>異常</th><th>代表意義</th></tr></thead>
    <tbody>
      <tr><td>總空中時間</td><td class="n g">&lt; 50%</td><td class="n a">50–70%</td><td class="n r">&gt; 70%</td>
        <td>頻道被佔用的比例,滿了就塞車</td></tr>
      <tr><td>他源干擾</td><td class="n g">&lt; 25%</td><td class="n a">25–40%</td><td class="n r">&gt; 40%</td>
        <td>非本台造成的佔用,外部訊號或鄰台</td></tr>
      <tr><td>TX 重試</td><td class="n g">&lt; 20%</td><td class="n a">20–30%</td><td class="n r">&gt; 30%</td>
        <td>傳輸失敗重送率,直接反映體感</td></tr>
      <tr><td>CPU</td><td class="n g">&lt; 60%</td><td class="n a">60–80%</td><td class="n r">&gt; 80%</td>
        <td>AP 處理器負載</td></tr>
      <tr><td>記憶體</td><td class="n g">&lt; 85%</td><td class="n a">85–90%</td><td class="n r">&gt; 90%</td>
        <td>過高可能觸發自動重開</td></tr>
    </tbody>
  </table></div>

  <div class="foot">
    <div><b>資料來源</b> UniFi OS API(m09829.hostifi.com · site {esc(D['site'])})每分鐘擷取一次</div>
    <div><b>備用機</b> {s['spares']} 台尚未佈署(未用數字編號命名),不列入告警</div>
    <div><b>DFS 說明</b> 頻道 52–144 屬 DFS,偵測到雷達訊號會被迫跳頻造成瞬斷,現場活動建議避開</div>
  </div>
</div>"""

# 自動重新整理(放在 f-string 外面,JS 的大括號才不會被當成格式符號)
HTML += """
<script>
// GitHub Pages 會讓瀏覽器快取 10 分鐘:每 60 秒帶時間戳記重新抓一次,只替換內容,不整頁閃爍
(function () {
  var busy = false, last = Date.now();
  function refresh() {
    if (busy) return;
    busy = true;
    var url = location.pathname + "?t=" + Date.now();
    // 現場 WiFi 塞的時候請求可能卡住:20 秒沒回應就放棄,下一輪重試
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 20000);
    fetch(url, { cache: "no-store", signal: ctl ? ctl.signal : undefined }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      var fresh = doc.querySelector(".wrap");
      var cur = document.querySelector(".wrap");
      if (fresh && cur) { cur.innerHTML = fresh.innerHTML; document.title = doc.title; last = Date.now(); checkStale(); }
    }).catch(function () {
      var el = document.getElementById("live");
      if (el) el.textContent = "更新失敗(這台裝置連不上網),稍後重試";
    }).then(function () { clearTimeout(timer); busy = false; });
  }
  // 資料超過 3 分鐘沒更新就標紅,現場一眼看得出監控是不是停了
  function checkStale() {
    var b = document.querySelector(".stamp b"), el = document.getElementById("live");
    if (!b || !el) return;
    var t = new Date(b.textContent.trim().replace(" ", "T") + "+08:00");
    var age = (Date.now() - t.getTime()) / 1000;
    if (age > 180) {
      el.textContent = "資料已 " + Math.floor(age / 60) + " 分鐘沒有更新";
      el.style.color = "var(--crit)"; el.style.fontWeight = "600";
    }
  }
  checkStale();
  setInterval(checkStale, 15000);
  // 手機鎖螢幕或切到別的 App 時,瀏覽器會暫停計時器;回到畫面時立刻抓一次
  function wake() { if (document.visibilityState !== "hidden" && Date.now() - last > 20000) refresh(); }
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("focus", wake);
  window.addEventListener("pageshow", wake);
  window.addEventListener("online", refresh);
  setInterval(function () { if (Date.now() - last >= 55000) refresh(); }, 15000);
})();
</script>"""

out = os.path.join(HERE, "dashboard.html")
open(out, "w").write(HTML)
print("寫入", out, len(HTML), "bytes")

idx = os.path.join(HERE, "index.html")
open(idx, "w").write(HTML)
print("寫入", idx, len(HTML), "bytes")
