#!/usr/bin/env python3
"""YC 松菸 WiFi 監控 - 從 UniFi OS(hostifi)抓取即時指標並產生報告資料"""
import json, os, re, ssl, sys, time, urllib.request, urllib.error, http.cookiejar
from datetime import datetime, timezone, timedelta

SITE_NAME = "YC 松菸"
HOST = "https://m09829.hostifi.com:11443"   # UniFi OS(Network 應用走 /proxy/network)
SITE = "x9ajowek"
DEPLOYED_NAMES = set()   # 不是數字命名、但有佈署在現場的 AP
VENUE_NET = "192.168."   # 活動現場網段(RB5009 LAN 192.168.88.0/22);172.16.x.x 是公司網段
NET = "/proxy/network"
# 認證二擇一:UNIFI_API_KEY(建議,不受 MFA 影響)或本機管理員帳密 UNIFI_USER/UNIFI_PASS
API_KEY = os.environ.get("YC_UNIFI_API_KEY") or os.environ.get("UNIFI_API_KEY", "")
USER = os.environ.get("YC_UNIFI_USER", "")
PASS = os.environ.get("YC_UNIFI_PASS", "")
# MikroTik:唯讀帳號 noc(只允許公司 IP 登入)。CCR 直連 8088;CRS418 經 CCR 的 18088 轉發
# 主機依序嘗試:先 DDNS(活動現場),再公司內網位址(設備還在公司時)
MT_USER = os.environ.get("YC_NOC_USER", "")
MT_PASS = os.environ.get("YC_NOC_PASS", "")
MT_ENABLED = bool(MT_USER and MT_PASS)
CCR_HOSTS = [h for h in os.environ.get("YC_CCR_HOSTS",
             "http://hgk09ry2t9c.sn.mynetname.net:8089").split(",") if h]
CRS_HOSTS = [h for h in os.environ.get("YC_CRS_HOSTS",
             "").split(",") if h]   # 現場未使用 CRS418,預設關閉;要啟用就設定 YC_CRS_HOSTS
# YC_CRS_HOSTS 設成空字串即可關閉 CRS418 監控(現場沒用到時)
CRS_ENABLED = MT_ENABLED and bool(CRS_HOSTS)
POE_OK = {"powered-on", "waiting-for-load", "disabled"}
# Alta 交換器:alta_watch.js 每 20 秒寫 alta.json;超過 ALTA_STALE 秒視為讀不到
ALTA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alta.json")
ALTA_STALE = 180
ALTA_POE_BUDGET = {"5": 240.0, "3": 60.0}   # model 5 = S24-POE、3 = S8-POE(Alta 後台顯示的總預算)
ALTA_MODEL = {"5": "Alta S24-POE", "3": "Alta S8-POE"}
TPE  = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ctx),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
)
_csrf = None

def call(path, payload=None):
    global _csrf
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if API_KEY:
        headers["X-API-KEY"] = API_KEY
    if _csrf:
        headers["X-CSRF-Token"] = _csrf
    req = urllib.request.Request(HOST + path, data=data, headers=headers,
                                 method="POST" if data else "GET")
    # HostiFi 偶爾回 503:同一輪內最多重試 2 次,間隔 5 秒,避免整輪資料空掉
    for attempt in range(3):
        try:
            with opener.open(req, timeout=30) as r:
                _csrf = r.headers.get("X-CSRF-Token") or _csrf
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(5)

def login():
    if API_KEY:
        return
    if not USER or not PASS:
        sys.exit("缺少 YC_UNIFI_API_KEY(或 YC_UNIFI_USER / YC_UNIFI_PASS)")
    call("/api/auth/login", {"username": USER, "password": PASS, "rememberMe": False})

def dfs(ch):
    return 52 <= ch <= 144


def chnum(v):
    """頻道轉成整數;'auto' 或空值回 0"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def poe_budget(d):
    """交換器目前可用的 PoE 總功率(W)。回傳 (瓦數, 是否為推估值)"""
    if d.get("total_max_power"):
        return float(d["total_max_power"]), False
    tbl = d.get("power_budget_table") or []
    src = str(d.get("power_source") or "")
    if src:
        hit = ([t for t in tbl if str(t.get("power_source")) == src and t.get("is_default")]
               or [t for t in tbl if str(t.get("power_source")) == src])
        if hit:
            return float(hit[0].get("avail_budget") or hit[0].get("power_budget") or 0), False
    dflt = [t for t in tbl if t.get("is_default")]
    if dflt:
        # 不知道目前供電來源時,假設接變壓器(預算最大的那組)
        return float(max(t.get("avail_budget") or 0 for t in dflt)), True
    return None, True


def ros_client(hosts):
    """依序嘗試 RouterOS REST 主機,回傳 (查詢函式, system/resource);都連不上回 (None, None)"""
    import base64
    auth = "Basic " + base64.b64encode(f"{MT_USER}:{MT_PASS}".encode()).decode()

    def make(base):
        def q(path, body=None):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": auth, "Content-Type": "application/json"},
                method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        return q

    for base in hosts:
        q = make(base)
        try:
            res = q("/rest/system/resource")
            if isinstance(res, dict) and res.get("board-name"):
                return q, res
        except Exception:
            continue
    return None, None


def health_of(q):
    try:
        return {h.get("name"): h.get("value") for h in q("/rest/system/health")}
    except Exception:
        return {}


def psu_failed(health):
    return sorted(k.replace("-state", "") for k, v in health.items()
                  if k.startswith("psu") and k.endswith("-state") and v != "ok")


def rate_mbps(rate):
    """'1Gbps' / '100Mbps' / '2.5Gbps' → Mbps"""
    if not rate:
        return 0
    r = str(rate)
    try:
        if r.endswith("Gbps"):
            return int(float(r[:-4]) * 1000)
        if r.endswith("Mbps"):
            return int(float(r[:-4]))
    except ValueError:
        pass
    return 0


def pool_capacity(ranges):
    """把 '192.168.88.20-192.168.91.200' 換算成可用位址總數"""
    def n(ip):
        a, b, c, d = (int(x) for x in ip.split("."))
        return ((a * 256 + b) * 256 + c) * 256 + d
    total = 0
    for seg in str(ranges).split(","):
        if "-" in seg:
            lo, hi = seg.split("-", 1)
            total += n(hi.strip()) - n(lo.strip()) + 1
        elif seg.strip():
            total += 1
    return total


def load_alta():
    """讀 alta_watch.js 的輸出;檔案不存在回 None,過期回 {"stale": True}"""
    try:
        d = json.load(open(ALTA_FILE))
    except (OSError, ValueError):
        return None
    if time.time() - d.get("written", 0) > ALTA_STALE:
        return {"stale": True, "switches": []}
    return d


def collect_ccr(infra_macs):
    """RB5009:對外線路、DHCP 用量、電源/溫度"""
    q, res = ros_client(CCR_HOSTS)
    if not q:
        return None
    health = health_of(q)
    members = [m.get("interface") for m in q("/rest/interface/list/member")
               if m.get("list") == "WAN" and m.get("disabled") != "true"]
    ifaces = {i.get("name"): i for i in q("/rest/interface")}
    running = [m for m in members if ifaces.get(m, {}).get("running") == "true"]
    # 以「實際生效的預設路由」決定對外線路。PPPoE 跑在 ether1 上時,ether1 有連線不代表能上網
    active_gw = None
    for r in q("/rest/ip/route"):
        if r.get("dst-address") == "0.0.0.0/0" and r.get("active") == "true":
            g = (r.get("immediate-gw") or r.get("gateway") or "")
            active_gw = g.split("%")[-1] if "%" in g else g
            break
    if active_gw in ifaces:
        wan = active_gw
        running = [wan] if ifaces[wan].get("running") == "true" else []
    else:
        # 沒有生效的預設路由:視為對外中斷;顯示時優先列 PPPoE
        ppp = [m for m in members if m.startswith("pppoe")]
        wan = (ppp or members or [None])[0]
        running = []
    down = up = None
    if running:
        t = q("/rest/interface/monitor-traffic", {"interface": wan, "once": ""})[0]
        down = round(int(t.get("rx-bits-per-second") or 0) / 1e6, 2)
        up = round(int(t.get("tx-bits-per-second") or 0) / 1e6, 2)

    servers = q("/rest/ip/dhcp-server")
    pool_name = next((x.get("address-pool") for x in servers if x.get("disabled") != "true"), "dhcp")
    pool_rng = next((p.get("ranges") for p in q("/rest/ip/pool") if p.get("name") == pool_name), "")
    cap = pool_capacity(pool_rng) if pool_rng else 0
    bound = [l for l in q("/rest/ip/dhcp-server/lease") if l.get("status") == "bound"]
    # AP、交換器這類網路設備的租約不算「人」
    infra = sum(1 for l in bound if (l.get("mac-address") or "").lower() in infra_macs)
    return {
        "ok": True,
        "board": res.get("board-name"), "version": res.get("version"),
        "cpu": int(res.get("cpu-load") or 0), "uptime": res.get("uptime"),
        "cpu_temp": int(health.get("cpu-temperature") or 0),
        "psu_fail": psu_failed(health),
        "wan_name": wan, "wan_running": bool(running), "wan_members": members,
        # 雙 WAN:每條 PPPoE 的撥號狀態,active 是目前實際走的那條
        "wans": [{"name": m, "running": ifaces.get(m, {}).get("running") == "true", "active": m == wan}
                 for m in members if m.startswith("pppoe")],
        "down_mbps": down, "up_mbps": up,
        "leases_bound": len(bound), "clients": len(bound) - infra, "infra": infra,
        "pool_range": pool_rng, "pool_capacity": cap,
        "pool_used_pct": round(len(bound) / cap * 100, 1) if cap else None,
    }


def collect_crs():
    """CRS418 核心交換器:每個埠的連線速率、PoE、LLDP 鄰居"""
    q, res = ros_client(CRS_HOSTS)
    if not q:
        return None
    health = health_of(q)
    eths = [e.get("name") for e in q("/rest/interface/ethernet")]
    mon = {m.get("name"): m for m in q("/rest/interface/ethernet/monitor", {"numbers": ",".join(eths), "once": ""})}
    poe_ports = [p.get("name") for p in q("/rest/interface/ethernet/poe")]
    poe = ({m.get("name"): m for m in q("/rest/interface/ethernet/poe/monitor",
                                         {"numbers": ",".join(poe_ports), "once": ""})} if poe_ports else {})
    nb = {}
    for n in q("/rest/ip/neighbor"):
        nb.setdefault((n.get("interface") or "").split(",")[0], n)
    stats = {i.get("name"): i for i in q("/rest/interface")}
    ports = []
    for nm in eths:
        m, pm, n, st = mon.get(nm, {}), poe.get(nm), nb.get(nm), stats.get(nm, {})
        ports.append({
            "name": nm,
            "up": m.get("status") == "link-ok",
            "rate": m.get("rate"), "rate_mbps": rate_mbps(m.get("rate")),
            "neighbor": n.get("identity") if n else None,
            "neighbor_mac": (n.get("mac-address") or "").lower() if n else None,
            "poe": pm is not None,
            "poe_status": pm.get("poe-out-status") if pm else None,
            "poe_w": float(pm.get("poe-out-power") or 0) if pm else None,
            "errors": int(st.get("rx-error") or 0) + int(st.get("tx-error") or 0) + int(st.get("fcs-error") or 0),
        })
    return {
        "ok": True,
        "board": res.get("board-name"), "version": res.get("version"),
        "cpu": int(res.get("cpu-load") or 0), "uptime": res.get("uptime"),
        "temp": max([int(v) for k, v in health.items() if k.endswith("temperature") and str(v).isdigit()] or [0]),
        "poe_total_w": float(health.get("poe-out-consumption") or 0),
        "psu_fail": psu_failed(health),
        "macs": [(b.get("mac-address") or "").lower() for b in q("/rest/interface/bridge")],
        "ports": ports,
    }


def main():
    login()
    devices = call(f"{NET}/api/s/{SITE}/stat/device")["data"]
    stations = call(f"{NET}/api/s/{SITE}/stat/sta")["data"]

    # MikroTik:RB5009 與 CRS418(沒設定唯讀帳號就跳過)
    infra_macs = {(d.get("mac") or "").lower() for d in devices}
    crs = mt = None
    if MT_ENABLED:
        try:
            crs = collect_crs() if CRS_ENABLED else None
        except Exception:
            crs = None
        if crs:
            infra_macs |= {p["neighbor_mac"] for p in crs["ports"] if p["neighbor_mac"]}
            infra_macs |= set(crs["macs"])
        try:
            mt = collect_ccr(infra_macs)
        except Exception:
            mt = None

    # 場地特性:與會者坐定不移動,不倚賴漫遊。弱訊號代表該座位涵蓋不足,
    # 不是「該漫遊卻沒漫遊」,所以要盯的是有沒有人被卡在收不到訊號的位置。
    WEAK_DBM = -75          # 低於此值視為座位涵蓋不足
    MINRATE_MBPS = 12       # 低於此連線速率視為慢速用戶
    weak_by_ap, slow_by_ap = {}, {}
    for c in stations:
        ap = (c.get("ap_mac") or "")
        if c.get("signal", 0) and c["signal"] < WEAK_DBM:
            weak_by_ap.setdefault(ap, []).append(c)
        rate = (c.get("rx_rate") or 0) / 1000
        if 0 < rate < MINRATE_MBPS:
            slow_by_ap.setdefault(ap, []).append(c)

    sw_name = {d.get("mac"): (d.get("name") or d.get("mac", "?")[-5:])
               for d in devices if d.get("type") == "usw"}
    port_ap, ap_port = {}, {}
    for d in devices:
        if d.get("type") != "uap":
            continue
        u = d.get("uplink") or d.get("last_uplink") or {}
        if u.get("uplink_mac") and u.get("uplink_remote_port"):
            nm = d.get("name") or d.get("mac", "?")[-5:]
            port_ap[(u["uplink_mac"], u["uplink_remote_port"])] = nm
            ap_port[d.get("mac")] = (u["uplink_mac"], u["uplink_remote_port"])

    # Alta 交換器:用 LLDP 鄰居表對應 AP 接在哪個埠
    alta = load_alta()
    for sw in (alta or {}).get("switches", []):
        sw_name[sw["mac"]] = sw["name"]
        for pt in sw["ports"]:
            nb = (pt.get("neighbor") or {}).get("mac", "")
            ap = next((d for d in devices if d.get("type") == "uap"
                       and (d.get("mac") or "").replace(":", "").lower() == nb), None)
            if ap:
                port_ap[(sw["mac"], pt["idx"])] = ap.get("name") or ap.get("mac", "?")[-5:]
                ap_port[ap.get("mac")] = (sw["mac"], pt["idx"])

    # 接在 CRS418 上的 AP(例如 E7):用 CRS 的 LLDP 鄰居表對應埠號
    if crs:
        sw_name["CRS418"] = "CRS418"
        for d in devices:
            if d.get("type") == "uap" and d.get("mac") not in ap_port:
                hit = next((x for x in crs["ports"] if x["neighbor_mac"] == (d.get("mac") or "").lower()), None)
                if hit:
                    ap_port[d.get("mac")] = ("CRS418", hit["name"])

    aps, offline, spares = [], [], []
    for d in devices:
        if d.get("type") != "uap":
            continue
        name = d.get("name") or d.get("mac", "?")[-5:]
        ip = d.get("ip") or ""
        # 現役機:數字編號命名,或列在 DEPLOYED_NAMES;其餘視為尚未佈署的備用機
        deployed = name.strip().isdigit() or name in DEPLOYED_NAMES
        if d.get("state") != 1:
            rec = {"name": name, "mac": d.get("mac"), "ip": ip, "at_venue": ip.startswith(VENUE_NET)}
            (offline if deployed else spares).append(rec)
            continue

        st = d.get("system-stats", {}) or {}
        mac = d.get("mac") or ""
        stats5 = [r for r in d.get("radio_table_stats", []) if r.get("radio") == "na"]
        cfg5 = [r for r in d.get("radio_table", []) if r.get("radio") == "na"] or [{}]
        # 雙 5GHz 機種(如 E7 Campus)每個無線電各列一筆。第二個以後標為 virtual:
        # 只檢查該無線電自己的指標,CPU、上行、弱訊號人數這類整機項目只在第一筆檢查
        for idx, c5 in enumerate(cfg5):
            r5 = next((r for r in stats5 if c5.get("name") and r.get("name") == c5.get("name")), None)
            if r5 is None:
                r5 = stats5[idx] if idx < len(stats5) else {}
            virtual = idx > 0
            cu_total = r5.get("cu_total") or 0
            self_use = (r5.get("cu_self_rx") or 0) + (r5.get("cu_self_tx") or 0)
            # 計數器剛重置時 tx_retries_pct 會出現 100% 之類的假值,樣本太小就不採信
            tx_packets = r5.get("tx_packets") or 0
            ch = chnum(r5.get("channel")) or chnum(c5.get("channel"))
            aps.append({
                "name": name if not virtual else f"{name} 5G-{idx + 1}",
                "device": name,
                "virtual": virtual,
                "radio": c5.get("name"),
                "ip": d.get("ip"),
                "weak": 0 if virtual else len(weak_by_ap.get(mac, [])),
                "slow": 0 if virtual else len(slow_by_ap.get(mac, [])),
                "weakest": None if virtual else min([c["signal"] for c in weak_by_ap.get(mac, [])], default=None),
                "uptime": d.get("uptime") or 0,
                "clients": r5.get("num_sta") or 0,
                "channel": ch,
                "bw": r5.get("bw") or c5.get("ht") or 0,
                "dfs": dfs(ch),
                "cfg_channel": chnum(c5.get("channel")),
                "radio_state": r5.get("state") or "",
                "tx_power": r5.get("tx_power") or 0,
                "tx_power_mode": c5.get("tx_power_mode") or "?",
                "max_txpower": c5.get("max_txpower") or 0,
                "min_rssi_on": bool(c5.get("min_rssi_enabled")),
                "min_rssi": c5.get("min_rssi"),
                "cpu": float(st.get("cpu") or 0),
                "mem": float(st.get("mem") or 0),
                "airtime": cu_total,
                "self_use": self_use,
                "interference": max(0, cu_total - self_use),
                "tx_retry": float(r5.get("tx_retries_pct") or 0),
                "retry_ok": tx_packets >= 5000,
                "up_sw": sw_name.get(ap_port.get(mac, (None,))[0]),
                "up_port": ap_port.get(mac, (None, None))[1],
                "up_speed": 0 if virtual else (d.get("uplink") or {}).get("speed") or 0,
            })

    aps.sort(key=lambda a: a["name"])

    # ---- 交換器 / PoE ----
    switches = []
    for d in devices:
        if d.get("type") != "usw":
            continue
        mac, ip = d.get("mac"), d.get("ip") or ""
        online = d.get("state") == 1
        ports = []
        for p in d.get("port_table", []):
            idx = p.get("port_idx")
            ports.append({
                "idx": idx,
                "ap": port_ap.get((mac, idx)),
                "up": bool(p.get("up")) if online else None,
                "speed": (p.get("speed") or 0) if online else None,
                "poe": bool(p.get("port_poe")),
                "poe_w": round(float(p.get("poe_power") or 0), 1) if online and p.get("port_poe") else None,
            })
        budget, est = poe_budget(d)
        used = round(sum(p["poe_w"] or 0 for p in ports), 1) if online else None
        switches.append({
            "name": sw_name.get(mac), "mac": mac, "ip": ip, "model": d.get("model"),
            "online": online, "at_venue": ip.startswith(VENUE_NET),
            "poe_used": used, "poe_budget": budget, "poe_budget_est": est,
            "poe_pct": round(used / budget * 100, 1) if online and budget else None,
            "ports": ports,
        })
    now = time.time()
    for sw in (alta or {}).get("switches", []):
        online = bool(sw.get("ts")) and now - sw["ts"] < ALTA_STALE
        # 只列有連線的埠(24 埠全列太長);非 AP 的鄰居用 LLDP 名稱標示,例如 RB5009、另一台交換器
        ports = [{
            "idx": pt["idx"], "ap": port_ap.get((sw["mac"], pt["idx"])),
            "label": ((pt.get("neighbor") or {}).get("name") or "") + ("(上行)" if pt.get("uplink") else ""),
            "up": pt["up"] if online else None,
            "speed": pt["speed"] if online else None,
            "poe": pt["poe"],
            "poe_w": round(pt["poe_w"], 1) if online and pt["poe"] and pt.get("poe_w") is not None else None,
        } for pt in sw["ports"] if not online or pt["up"]]
        budget = ALTA_POE_BUDGET.get(str(sw.get("model")))
        used = round(sum(p["poe_w"] or 0 for p in ports), 1) if online else None
        switches.append({
            "name": sw["name"], "mac": sw["mac"], "ip": sw.get("ip") or "", "model": f"{ALTA_MODEL.get(str(sw.get('model')), 'Alta')} · {sum(1 for p in sw['ports'] if p['up'])}/{sw.get('nports')} 埠使用中",
            "online": online, "at_venue": (sw.get("ip") or "").startswith(VENUE_NET),
            "poe_used": used, "poe_budget": budget, "poe_budget_est": False,
            "poe_pct": round(used / budget * 100, 1) if online and budget else None,
            "temp": sw.get("temp"), "ports": ports,
        })
    switches.sort(key=lambda x: x["name"] or "")
    sw_by_mac = {x["mac"]: x for x in switches}

    # ---- 問題判讀(建議一律給到可直接執行的具體值)----
    issues = []
    def add(sev, title, detail, fix, key=None):
        # key 用來辨識「同一件事」:把標題中會變動的數值換成 #,保留 AP 編號
        issues.append({"sev": sev, "title": title, "detail": detail, "fix": fix,
                       "key": key or re.sub(r"(?<!AP )(?<!\d)\d+(?:\.\d+)?", "#", title)})

    NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]
    # 40MHz 綁定搭檔(ch165 無法綁定)
    BOND = {36: 40, 40: 36, 44: 48, 48: 44, 149: 153, 153: 149, 157: 161, 161: 157}
    used = [a["channel"] for a in aps]

    def free_channel(taken):
        """回傳一個目前沒人用的非 DFS 頻道;全滿則挑空中時間最低那台所用的頻道"""
        for c in NON_DFS:
            if c not in taken:
                return c, True
        quiet = min(aps, key=lambda x: x["airtime"])
        return quiet["channel"], False

    def relief_aps(exclude):
        """有餘裕可以接手用戶的 AP:空中時間低且用戶少"""
        c = [x for x in aps if x["name"] != exclude and x["airtime"] < 30 and x["clients"] < 12]
        c.sort(key=lambda x: (x["clients"], x["airtime"]))
        return [x["name"] for x in c[:2]]

    # 撞頻:直接指名把哪一台改到哪一個頻道
    chans = {}
    for a in aps:
        chans.setdefault(a["channel"], []).append(a)
    # 非 DFS 頻道全用完時,無線電比頻道多,共用頻道是必然的,不告警;
    # 實際的干擾若真的造成影響,會反映在空中時間/他源干擾的告警上
    free_left = [c for c in NON_DFS if c not in used]
    for ch, group in sorted(chans.items()):
        if len(group) > 1 and ch and free_left:
            # 用戶少的那台搬家,影響最小
            group.sort(key=lambda x: x["clients"])
            mover = group[0]
            taken = list(used)
            newch, isfree = free_channel(taken)
            others = "、".join(f"AP {g['name']}" for g in group[1:])
            add("crit", f"頻道 {ch} 撞頻:AP {mover['name']} 與 {others}",
                f"共用 ch{ch},彼此同頻干擾。AP {mover['name']} 目前 {mover['clients']} 人,是影響最小的搬移對象。",
                f"把 AP {mover['name']} 的 5GHz 頻道從 ch{ch} 改成 ch{newch}"
                + ("(目前無人使用)。" if isfree else "(全站頻道已用滿,此為最空閒的一個)。")
                + f" 路徑:UniFi → 裝置 → AP {mover['name']} → 設定 → 無線電 → 5 GHz 頻道。")
            used.append(newch)

    for a in aps:
        n, ch = a["name"], a["channel"]

        # 空中時間過高:先分辨是自身流量還是外部干擾,兩者解法相反
        if a["airtime"] >= 50:
            sev = "crit" if a["airtime"] >= 70 else "warn"
            self_ratio = a["self_use"] / a["airtime"] if a["airtime"] else 0
            if self_ratio >= 0.6:
                # 自身流量塞滿 → 容量問題,換頻道無效
                helpers = relief_aps(n)
                newp = max(6, a["tx_power"] - 3)
                steps = []
                # 40MHz 會綁定相鄰頻道,必須確認搭檔頻道沒被別台佔用
                partner = BOND.get(ch)
                if a["bw"] == 20 and partner:
                    occupant = next((x for x in aps if x["channel"] == partner and x["name"] != n), None)
                    if occupant is None:
                        steps.append(f"把 AP {n} 的 5GHz 頻寬從 20MHz 改成 40MHz(ch{ch} 會綁定 ch{partner},"
                                     f"目前無人使用),可用容量加倍")
                    else:
                        spare, isfree = free_channel(used)
                        if isfree:
                            steps.append(f"先把 AP {occupant['name']} 從 ch{partner} 移到 ch{spare} 空出頻道,"
                                         f"再把 AP {n} 頻寬改成 40MHz(ch{ch}+ch{partner}),容量加倍"
                                         f"(注意:AP {occupant['name']} 目前有 {occupant['clients']} 人,會瞬斷一次)")
                        else:
                            steps.append(f"頻寬維持 20MHz(ch{ch} 要升 40MHz 需綁定 ch{partner},"
                                         f"但已被 AP {occupant['name']} 佔用,且無空閒頻道可挪)")
                # 與會者坐定不移動,降功率不會讓人轉台,只會讓原座位收訊變差 → 不建議
                steps.append(f"若仍不足,在 AP {n} 涵蓋的座位區加掛一台備用機分攤"
                             f"(現有 {len(spares)} 台備用機可用),這是固定座位場地唯一能真正加容量的做法")
                add(sev, f"AP {n} 空中時間 {a['airtime']}%(自身流量佔 {a['self_use']}%)",
                    f"{a['clients']} 位用戶掛在這台,{a['self_use']}% 是自己的傳輸、只有 {a['interference']}% 是外部干擾。"
                    "與會者坐定不移動,代表人就是集中在這台涵蓋的座位區,換頻道與降功率都沒有幫助。",
                    "依序執行:" + " ".join(f"{i+1}) {s}。" for i, s in enumerate(steps))
                    + f" 路徑:UniFi → 裝置 → AP {n} → 設定 → 無線電。")
            else:
                newch, isfree = free_channel(used)
                add(sev, f"AP {n} 空中時間 {a['airtime']}%(外部干擾佔 {a['interference']}%)",
                    f"只有 {a['self_use']}% 是自身流量,其餘為外部訊號佔用,ch{ch} 這個頻道環境很髒。",
                    f"把 AP {n} 的 5GHz 頻道從 ch{ch} 改成 ch{newch}"
                    + ("(目前無人使用)。" if isfree else "(全站已用滿,此為最空閒者)。")
                    + f" 路徑:UniFi → 裝置 → AP {n} → 設定 → 無線電 → 5 GHz 頻道。")
                used.append(newch)

        if a["retry_ok"] and a["tx_retry"] >= 20:
            sev = "crit" if a["tx_retry"] >= 30 else "warn"
            add(sev, f"AP {n} TX 重試 {a['tx_retry']:.1f}%",
                f"每 100 個封包有 {a['tx_retry']:.0f} 個要重送。這台目前有 {a['weak']} 位用戶訊號低於 -75 dBm,"
                "座位離 AP 太遠是主因。",
                f"確認 AP {n} 涵蓋的座位區有無死角(牆柱遮蔽、AP 被布幔擋住)。"
                "與會者不會移動,唯一解是把 AP 移近人群或在該區補一台,不要用降功率的方式處理。")

        # 固定座位場地的關鍵風險:有人被卡在收不到訊號的座位
        if a["weak"] >= 3:
            add("warn", f"AP {n} 有 {a['weak']} 位用戶訊號低於 -75 dBm(最弱 {a['weakest']} dBm)",
                "座位涵蓋不足,這些用戶容易速度慢或斷線。",
                f"到 AP {n} 涵蓋的座位區實測訊號,找出死角;必要時把備用機架在該區。"
                "若 SSID 有設最低資料速率,大量出現時先把它切回自動"
                "(UniFi → 設定 → WiFi → 該 SSID → 進階 → Minimum Data Rate Control),以免有人完全無法上網。")
        if a["slow"] >= 2:
            add("warn", f"AP {n} 有 {a['slow']} 位用戶速率低於 12 Mbps",
                "連線速率偏低,通常是訊號弱或裝置較舊,會拖慢整台 AP 的空中時間。",
                "檢查這些用戶所在座位的訊號;若人數持續增加,考慮在該區補 AP。")

        if a["interference"] >= 25 and a["airtime"] < 50:
            sev = "crit" if a["interference"] >= 40 else "warn"
            newch, isfree = free_channel(used)
            add(sev, f"AP {n} 他源干擾 {a['interference']}%",
                f"ch{ch} 有外部訊號源在佔用空中時間,不是本台造成的。",
                f"把 AP {n} 的 5GHz 頻道從 ch{ch} 改成 ch{newch}"
                + ("(目前無人使用)。" if isfree else "(全站已用滿,此為最空閒者)。")
                + f" 若換完仍高,代表整段 5GHz 都被鄰近單位佔用,屬環境限制。")
            used.append(newch)

        if not a["virtual"] and a["cpu"] >= 80:
            add("warn", f"AP {n} CPU {a['cpu']:.0f}%",
                f"處理器負載偏高,目前 {a['clients']} 位用戶。",
                f"若持續超過 80% 並伴隨掉線,把 AP {n} 重開機:UniFi → 裝置 → AP {n} → 設定 → 重新啟動。")
        if not a["virtual"] and a["mem"] >= 90:
            add("warn", f"AP {n} 記憶體 {a['mem']:.0f}%",
                "記憶體吃緊,U7 Lite 在此區間可能觸發自動重開。",
                f"安排離峰時段重開 AP {n}:UniFi → 裝置 → AP {n} → 設定 → 重新啟動。")

        # DFS 是刻意用來拆開撞頻的(10/9 19:02 起 AP 5/8/10),平常不告警;
        # 只有設定在 DFS、實際卻跑在別的頻道(偵測到雷達被迫跳走)或正在靜默偵測時才告警
        cfg = a.get("cfg_channel")
        if cfg and dfs(cfg) and a.get("radio_state") == "DFS_WAIT":
            add("info", f"AP {n} 正在 DFS 頻道 ch{cfg} 偵測雷達(約 60 秒)",
                "切到 DFS 頻道後要先靜默偵測雷達才會開始發射,這段時間這台沒有訊號。",
                "等 1–2 分鐘即可,超過 5 分鐘仍沒恢復再處理。", key=f"dfs-cac-{n}")
        elif cfg and dfs(cfg) and ch != cfg:
            add("warn", f"AP {n} 偵測到雷達,已從 DFS ch{cfg} 跳到 ch{ch}",
                f"DFS 規定偵測到雷達要立刻離開,30 分鐘內不能回 ch{cfg}。跳頻當下這台的用戶會斷線重連。",
                f"不用處理,30 分鐘後 AP 會自動回 ch{cfg}。若一直反覆發生,把 AP {n} 改到其他 DFS 頻道"
                f"(避開 120–128)或改回非 DFS 頻道。", key=f"dfs-radar-{n}")

        if not a["virtual"] and a["uptime"] < 900:
            add("info", f"AP {n} 剛重開機({a['uptime']//60} 分鐘前)",
                "剛套用設定變更會重啟屬正常;若無人調整則是異常重啟。",
                f"若近期沒有改過設定,到 UniFi → 裝置 → AP {n} → 洞察 查看重啟原因,"
                "並確認 PoE 供電是否穩定。")

    # 註:本場地與會者坐定不移動,不建議啟用 Minimum RSSI。
    # 該機制是把弱訊號用戶踢掉逼其改連較近的 AP;人不移動時對方找不到更好的 AP,
    # 只會重連回同一台,形成反覆斷線迴圈,反而比原本更糟。

    for o in offline:
        if not o["at_venue"]:
            # 最後一次出現在公司網段:代表還沒接上現場網路,不是現場斷線
            add("info", f"AP {o['name']} 尚未接上現場網路",
                f"最後出現在公司網段 {o['ip'] or '未知'},尚未在現場 192.168.88.x 網段上線過。",
                f"到場佈署時接上現場 PoE switch,開機後應取得 192.168.88.x 位址;"
                f"若上線後仍是 172.16.x.x,代表接錯網路或 VLAN 設定有誤。")
            continue
        # 從上游交換器的埠狀態判斷是哪一段出問題
        sw_mac, port = ap_port.get(o["mac"], (None, None))
        sw = sw_by_mac.get(sw_mac)
        where = f"(接在 {sw['name']} 第 {port} 埠)" if sw and port else ""
        if sw and not sw["online"]:
            diag = f"上游交換器 {sw['name']} 也離線,先處理交換器:確認電源與上行網路線。"
        elif sw and port:
            pt = next((x for x in sw["ports"] if x["idx"] == port), None)
            if pt and not pt["up"]:
                diag = f"{sw['name']} 第 {port} 埠沒有連線:網路線鬆脫、斷線,或 AP 端沒接好。"
            elif pt and pt["poe"] and not pt["poe_w"]:
                diag = f"{sw['name']} 第 {port} 埠有連線但沒有輸出 PoE 電力:確認該埠 PoE 是否被關閉,或換一個埠測試。"
            else:
                diag = f"{sw['name']} 第 {port} 埠有連線也有供電,AP 本身可能當機,到現場把網路線拔插一次讓它重開。"
        else:
            diag = "檢查該台接的 PoE switch 埠是否供電、網路線是否鬆脫。"
        add("crit", f"AP {o['name']} 離線",
            f"現役機 {o['ip'] or '未知'}{where} 失聯,該區域目前沒有訊號涵蓋。",
            f"1) {diag}"
            f" 2) 若確認硬體故障,拿備用機更換(備用機已在控制器中,接上後改名為 {o['name']} 即可套用相同設定)。")

    # ---- 交換器 / PoE 告警 ----
    for sw in switches:
        n = sw["name"]
        hung = [p["ap"] for p in sw["ports"] if p["ap"]]
        if not sw["online"]:
            if sw["at_venue"]:
                add("crit", f"交換器 {n} 離線",
                    f"最後位址 {sw['ip'] or '未知'}。接在它下面的 AP {('、'.join(hung)) or '(未知)'} 都會跟著斷線。",
                    "到現場確認交換器電源指示燈;檢查變壓器或 PoE 輸入,以及往 MikroTik 的上行網路線。",
                    key=f"sw_offline:{n}")
            else:
                add("info", f"交換器 {n} 尚未接上現場網路",
                    f"最後出現在公司網段 {sw['ip'] or '未知'}。",
                    "到場佈署接上現場網路後,應取得 192.168.88.x 位址。", key=f"sw_staging:{n}")
            continue
        if sw["poe_pct"] is not None and sw["poe_pct"] >= 80:
            sev = "crit" if sw["poe_pct"] >= 90 else "warn"
            add(sev, f"交換器 {n} PoE 用量 {sw['poe_pct']}%",
                f"已用 {sw['poe_used']} W / 預算 {sw['poe_budget']:g} W"
                + ("(預算為推估值)" if sw["poe_budget_est"] else "") + "。超過預算時交換器會關掉部分埠的供電,AP 會無預警斷線。",
                "把部分 AP 移到另一台交換器,或改用變壓器供電給交換器以提高 PoE 預算。",
                key=f"poe_budget:{n}")

    if alta is None or alta.get("stale"):
        add("warn", "Alta 交換器資料讀不到",
            "alta_watch.js 沒有在跑,或超過 3 分鐘沒更新;交換器與 PoE 狀態暫時無法顯示。",
            "在 Mac mini 執行 screen -ls 確認 yc-alta 還在,並看 ~/yc-wifi-dashboard/alta.log。", key="alta_stale")

    # AP 上行速率不足 1Gbps:多半是網路線或水晶頭有問題
    for a in aps:
        if 0 < a["up_speed"] < 1000:
            where = f"{a['up_sw']} 第 {a['up_port']} 埠" if a["up_sw"] else "上游交換器"
            add("warn", f"AP {a['name']} 上行只有 {a['up_speed']} Mbps",
                f"接在 {where},應該要跑 1000 Mbps 以上。速率降到 100 Mbps 通常是網路線或水晶頭有一對線斷了,"
                "人多時這台會成為瓶頸。",
                f"更換 AP {a['name']} 的網路線,或重新壓水晶頭;換完到 UniFi 確認該埠回到 1 Gbps。",
                key=f"uplink_speed:{a['name']}")

    # 線上但拿到公司網段位址:接錯網路
    for a in aps:
        if not a["virtual"] and a["ip"] and not str(a["ip"]).startswith(VENUE_NET):
            add("warn", f"AP {a['name']} 使用公司網段 {a['ip']}",
                "這台 AP 在線,但位址不在現場 192.168.88.x 網段。",
                f"確認 AP {a['name']} 接的是現場 switch,以及上游 VLAN／DHCP 設定是否正確。")

    # ---- MikroTik RB5009 ----
    if MT_ENABLED and mt is None:
        add("crit", "MikroTik RB5009 無法連線",
            "路由器 REST API 沒有回應,DHCP 與對外頻寬狀態無法取得。",
            "確認路由器是否斷電、對外線路是否中斷。若現場網路正常,多半是 DDNS 還沒更新到新位址:"
            "在 RB5009 上執行 /ip cloud force-update。", key="ccr_down")
    elif mt:
        if not mt["wan_running"]:
            add("crit", f"對外線路中斷({mt['wan_name'] or 'WAN'})",
                "RB5009 的對外介面沒有連線,全場無法上網。",
                "檢查對外線路與 ISP 狀態;PPPoE 的話確認撥號狀態與帳密。", key="wan_down")
        for w in mt.get("wans", []):
            if not w["running"] and not w["active"]:
                add("warn", f"備援線路 {w['name']} 中斷",
                    "主線還在,但另一條 PPPoE 沒有撥上;主線再斷就會全場斷網。",
                    f"在 RB5009 執行 /interface pppoe-client print 看 {w['name']} 狀態,確認那條網路線與數據機。",
                    key=f"wan_backup:{w['name']}")
        if mt["pool_used_pct"] is not None and mt["pool_used_pct"] >= 80:
            add("crit", f"DHCP 位址池用量 {mt['pool_used_pct']}%",
                f"已配發 {mt['leases_bound']} / {mt['pool_capacity']} 個位址,快用完了。",
                "擴大位址池範圍,或縮短租期加速回收。", key="dhcp_pool")
        elif mt["pool_used_pct"] is not None and mt["pool_used_pct"] >= 60:
            add("warn", f"DHCP 位址池用量 {mt['pool_used_pct']}%",
                f"已配發 {mt['leases_bound']} / {mt['pool_capacity']} 個位址。", "持續觀察成長速度。", key="dhcp_pool")
        for psu in mt["psu_fail"]:
            add("warn", f"RB5009 電源 {psu[3:]} 異常",
                "這台有兩顆備援電源,目前其中一顆沒有供電;另一顆再出問題就會整台斷電。",
                "確認這顆電源的電源線有插上、插座有電;插好仍異常代表電源模組故障。", key=f"ccr_{psu}")
        if mt["cpu_temp"] >= 85:
            add("warn", f"RB5009 CPU 溫度 {mt['cpu_temp']}°C", "溫度偏高。",
                "確認風扇正常、機櫃通風,不要被其他設備堵住出風口。", key="ccr_temp")
        if mt["cpu"] >= 80:
            add("warn", f"RB5009 CPU {mt['cpu']}%", "路由器處理器負載偏高,可能影響全場速度。",
                "到 /tool profile 看是哪個功能吃 CPU。", key="ccr_cpu")

    # ---- CRS418 核心交換器 ----
    if CRS_ENABLED and crs is None:
        add("crit", "核心交換器 CRS418 無法連線",
            "讀不到 CRS418 的狀態,接在它下面的設備(例如 E7)可能一起斷線。",
            "確認 CRS418 電源,以及它接到 RB5009 的那條網路線;若現場網路正常,檢查 CCR 上往 CRS 的 18088 轉發規則。",
            key="crs_down")
    elif crs:
        for pt in crs["ports"]:
            who = f"(接 {pt['neighbor']})" if pt["neighbor"] else ""
            if pt["poe"] and pt["poe_status"] and pt["poe_status"] not in POE_OK:
                add("crit", f"CRS418 {pt['name']} PoE 異常:{pt['poe_status']}",
                    f"{pt['name']}{who} 的 PoE 供電出問題,接在這埠的設備會斷電。",
                    "拔掉該埠的設備檢查網路線;short-circuit 多半是線材或水晶頭損壞,"
                    "overload 代表設備耗電超過該埠上限,改接其他供電方式。", key=f"crs_poe:{pt['name']}")
            if pt["up"] and pt["neighbor"] and 0 < pt["rate_mbps"] < 1000:
                add("warn", f"CRS418 {pt['name']} 只跑 {pt['rate']}",
                    f"{pt['name']}{who} 的連線速率低於 1 Gbps,通常是網路線或水晶頭有問題。",
                    "更換這條網路線,換完確認回到 1 Gbps。", key=f"crs_rate:{pt['name']}")
        for psu in crs["psu_fail"]:
            add("warn", f"CRS418 電源 {psu[3:]} 異常",
                "這台有兩顆備援電源,目前其中一顆沒有供電;另一顆再出問題,接在它下面的設備會全部斷線。",
                "確認這顆電源的電源線有插上、插座有電;插好仍異常代表電源模組故障。", key=f"crs_{psu}")
        if crs["temp"] >= 80:
            add("warn", f"CRS418 溫度 {crs['temp']}°C", "溫度偏高。",
                "確認風扇正常、機櫃通風。", key="crs_temp")

    order = {"crit": 0, "warn": 1, "info": 2}
    # 同嚴重度下,交換器離線排最前面(它是底下 AP 斷線的根因)
    issues.sort(key=lambda i: (order[i["sev"]], not i["key"].startswith("sw_offline")))

    out = {
        "ts": datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S"),
        "site": SITE_NAME,
        "ssids": sorted({c.get("essid") for c in stations if c.get("essid")}),
        "mikrotik": mt,
        "crs": crs,
        "mt_configured": MT_ENABLED,
        "crs_configured": CRS_ENABLED,
        "aps": aps,
        "offline": offline,
        "spares": spares,
        "switches": switches,
        "issues": issues,
        "summary": {
            "online": sum(1 for a in aps if not a["virtual"]),
            "offline": len(offline),
            "spares": len(spares),
            "switches": len(switches),
            "switches_online": sum(1 for x in switches if x["online"]),
            "clients": sum(a["clients"] for a in aps),
            "crit": sum(1 for i in issues if i["sev"] == "crit"),
            "warn": sum(1 for i in issues if i["sev"] == "warn"),
        },
    }
    with open(os.path.join(HERE, "data.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # 歷史趨勢:每台 AP 的空中時間/重試率,保留最近 144 筆(10 分鐘 x 24 小時)
    hpath = os.path.join(HERE, "history.json")
    try:
        hist = json.load(open(hpath))
    except Exception:
        hist = []
    hist.append({
        "ts": out["ts"][11:16],
        "clients": out["summary"]["clients"],
        "ap": {a["name"]: {"air": a["airtime"], "retry": round(a["tx_retry"], 1)} for a in aps},
    })
    hist = hist[-144:]
    with open(hpath, "w") as f:
        json.dump(hist, f, ensure_ascii=False)
    out["history"] = hist
    with open(os.path.join(HERE, "data.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{out['ts']}  線上 {out['summary']['online']}  用戶 {out['summary']['clients']}  "
          f"嚴重 {out['summary']['crit']}  警告 {out['summary']['warn']}")

if __name__ == "__main__":
    main()
