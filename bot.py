#!/usr/bin/env python3
"""
paper-bot: elige monedas graduadas de pump.fun el solo y opera con DINERO SIMULADO.

No toca ninguna wallet ni pide claves: solo lee datos publicos y apunta lo que
habria pasado, con comisiones y slippage incluidos. Sirve para medir si un filtro
selecciona mejor que comprar sin filtro (grupo "control") antes de arriesgar nada.

Uso (solo Python 3.9+, sin instalar nada):
    python bot.py tick            una pasada (para cron / GitHub Actions)
    python bot.py loop --cada 60  pasadas continuas cada 60 s (--minutos N para parar tras N minutos)
    python bot.py informe         regenera el informe sin consultar nada

Salida en la carpeta --dir (por defecto ./datos):
    estado.json       memoria del bot
    operaciones.csv   operaciones cerradas
    index.html        informe para abrir en el movil
Para cambiar reglas, crea <dir>/config.json con las claves de CFG que quieras pisar.
"""
import argparse
import csv
import hashlib
import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CFG = {
    "stake_usd": 10.0,                      # importe simulado por operacion
    "fees": {"bot_pct": 1.0,                # comision del bot comercial, por lado
             "pool_pct": 0.30,              # comision del pool, por lado
             "slippage_pct": 1.0,           # deslizamiento por lado
             "tx_usd": 0.05},               # coste fijo por transaccion
    "exit": {"tp_mult": 2.0,                # objetivo: x2...
             "tp_fraction": 1.0,            # ...y al tocarlo se vende todo
             "stop_pct": 30.0,              # stop a -30% desde la entrada
             "stop_after_tp_mult": 1.0,     # solo si tp_fraction < 1: stop del resto en la entrada
             "stop_extra_slippage_pct": 5.0,  # un stop se ejecuta peor que su nivel
             "max_hours": 24.0},            # cierre por tiempo
    # salidas que se prueban a la vez sobre las mismas entradas (pisan los valores de "exit")
    "exits": {"x2_24h": {},
              "x2_1h": {"max_hours": 1.0},
              "rapida_1h": {"tp_mult": 1.5, "stop_pct": 20.0, "max_hours": 1.0}},
    "universe": {"dex": "pumpswap", "max_pair_age_h": 24.0, "max_watch": 1500},
    "strategies": {
        # compra sin filtro una de cada N graduadas, una hora despues de graduarse
        "control": {"age_min": [60, 120], "one_in": 4,
                    "sources": ["gt_nuevos", "pump_recientes"]},
        # filtros publicos tipicos
        "basico": {"age_min": [30, 1440], "mc_min": 20000, "mc_max": 2000000, "liq_min": 10000,
                   "need_x": True, "need_tg": True},
        # basico + senales de que sigue entrando gente
        "impulso": {"age_min": [30, 1440], "mc_min": 20000, "mc_max": 2000000, "liq_min": 10000,
                    "need_x": True, "need_tg": True,
                    "chg_h1_min": 0.0, "chg_m5_min": -10.0, "buy_sell_h1_min": 1.2,
                    "txns_h1_min": 60, "vol_h1_min": 3000, "mc_vs_max_min": 0.5},
    },
    "min_trades_verdict": 50,
    "gt_candles": True,           # afinar salidas con velas de 1 min si pasan >3 min entre pasadas
    "gt_max_calls": 25,
}

UA = "Mozilla/5.0 (paper-bot; solo lectura)"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/"
PUMP = "https://frontend-api-v3.pump.fun/"


DEFAULT_EXIT = "x2_24h"


def now():
    return time.time()


def exit_cfg(cfg, name):
    return {**cfg["exit"], **cfg.get("exits", {}).get(name or DEFAULT_EXIT, {})}


def exit_label(cfg, name):
    ex = exit_cfg(cfg, name)
    return f"x{ex['tp_mult']:g} · −{ex['stop_pct']:g}% · {ex['max_hours']:g} h"


def log(msg):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), msg, flush=True)


def http_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if i < tries - 1 and (e.code == 429 or e.code >= 500):
                time.sleep(5 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ValueError):
            if i < tries - 1:
                time.sleep(2)
                continue
            raise


def try_json(url, what):
    try:
        return http_json(url)
    except Exception as e:  # una fuente caida no debe parar la pasada
        log(f"  aviso: {what} no responde ({type(e).__name__}: {e})")
        return None


def num(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


# ---------------------------------------------------------------- descubrimiento

def discover_gt():
    """Pools recien creados en Solana (todas las plataformas); nos quedamos con los de pump."""
    out = []
    for page in (1, 2, 3):
        d = try_json(f"{GT}new_pools?page={page}", "GeckoTerminal nuevos pools")
        if not d:
            break
        for it in d.get("data") or []:
            try:
                rel = it["relationships"]
                if "pump" not in rel["dex"]["data"]["id"]:
                    continue
                mint = rel["base_token"]["data"]["id"].split("_", 1)[1]
                out.append({"mint": mint, "pool": it["attributes"]["address"], "source": "gt_nuevos"})
            except (KeyError, IndexError, TypeError):
                continue
        time.sleep(2.2)
    return out


def _pump_items(d, source):
    out = []
    for c in d if isinstance(d, list) else []:
        if not isinstance(c, dict) or not c.get("complete") or not c.get("mint"):
            continue
        out.append({"mint": c["mint"], "pool": c.get("pump_swap_pool") or c.get("pool_address"),
                    "source": source, "symbol": c.get("symbol"),
                    "x": bool(c.get("twitter")), "tg": bool(c.get("telegram"))})
    return out


def discover_pump():
    out = []
    d = try_json(PUMP + "coins?offset=0&limit=50&sort=last_trade_timestamp&order=DESC"
                 "&includeNsfw=false&complete=true", "pump.fun recientes")
    out += _pump_items(d, "pump_recientes")
    d = try_json(PUMP + "coins/currently-live?limit=50&offset=0&includeNsfw=false", "pump.fun en directo")
    out += _pump_items(d, "pump_directo")
    return out


# ---------------------------------------------------------------- datos de mercado

def pick_pair(pairs, mint, dex, pool=None):
    best = None
    for p in pairs:
        if p.get("dexId") != dex or (p.get("baseToken") or {}).get("address") != mint:
            continue
        if pool and p.get("pairAddress") == pool:
            return p
        if best is None or num((p.get("liquidity") or {}).get("usd")) > num((best.get("liquidity") or {}).get("usd")):
            best = p
    return best


def snapshot(mints, pools, dex):
    """Foto de mercado por moneda (DexScreener, 30 monedas por llamada)."""
    snap, ok = {}, False
    mints = list(mints)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        d = try_json(DEX + ",".join(chunk), "DexScreener")
        if d is None:
            continue
        ok = True
        pairs = d if isinstance(d, list) else (d.get("pairs") or [])
        for m in chunk:
            p = pick_pair(pairs, m, dex, pools.get(m))
            if not p or num(p.get("priceUsd")) <= 0:
                continue
            tx = (p.get("txns") or {}).get("h1") or {}
            chg = p.get("priceChange") or {}
            soc = {s.get("type") for s in ((p.get("info") or {}).get("socials") or []) if isinstance(s, dict)}
            snap[m] = {
                "price": num(p.get("priceUsd")), "mc": num(p.get("marketCap") or p.get("fdv")),
                "liq": num((p.get("liquidity") or {}).get("usd")),
                "created": num(p.get("pairCreatedAt")) / 1000.0, "pool": p.get("pairAddress"),
                "symbol": (p.get("baseToken") or {}).get("symbol") or "?",
                "chg_m5": num(chg.get("m5")), "chg_h1": num(chg.get("h1")),
                "buys_h1": num(tx.get("buys")), "sells_h1": num(tx.get("sells")),
                "vol_h1": num((p.get("volume") or {}).get("h1")),
                "x": "twitter" in soc, "tg": "telegram" in soc,
            }
    return snap, ok


def gt_bars(pool, mint, since, until):
    """Velas de 1 min cerradas con inicio >= since. [] si la fuente falla."""
    d = try_json(f"{GT}pools/{pool}/ohlcv/minute?aggregate=1&limit=1000"
                 f"&before_timestamp={int(until)}&currency=usd&token={mint}", "GeckoTerminal velas")
    try:
        rows = d["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        return []
    bars = []
    for r in rows:
        try:
            ts, o, h, l, c = int(r[0]), num(r[1]), num(r[2]), num(r[3]), num(r[4])
        except (IndexError, TypeError, ValueError):
            continue
        if ts >= since and ts + 60 <= until and min(o, h, l, c) > 0:
            bars.append((ts + 60, o, h, l, c))
    bars.sort()
    return bars


# ---------------------------------------------------------------- operaciones simuladas

def open_position(strat, mint, s, cfg, t, exit_name=DEFAULT_EXIT):
    f = cfg["fees"]
    stake = cfg["stake_usd"]
    p_buy = s["price"] * (1 + f["slippage_pct"] / 100)
    units = stake * (1 - (f["bot_pct"] + f["pool_pct"]) / 100) / p_buy
    return {"id": f"{strat}/{exit_name}:{mint}", "strat": strat, "exit": exit_name, "mint": mint, "pool": s["pool"], "symbol": s["symbol"],
            "t_in": t, "p_ref": s["price"], "mc_in": s["mc"], "units": units, "stake": stake,
            "frac_left": 1.0, "tp_done": False, "proceeds": 0.0, "n_tx": 1, "last_check": t,
            "last_price": s["price"], "last_seen": t, "status": "abierta", "reason": "",
            "ambiguous": False, "events": []}


def _sell(pos, frac, base_price, extra_slip, cfg, ts, reason):
    f = cfg["fees"]
    px = base_price * (1 - extra_slip / 100) * (1 - f["slippage_pct"] / 100)
    pos["proceeds"] += pos["units"] * frac * px * (1 - (f["bot_pct"] + f["pool_pct"]) / 100)
    pos["n_tx"] += 1
    pos["frac_left"] = round(pos["frac_left"] - frac, 9)
    pos["events"].append([int(ts), reason, frac, base_price])
    if pos["frac_left"] <= 1e-9:
        pos["frac_left"] = 0.0
        pos["status"] = "cerrada"
        pos["reason"] = reason
        pos["t_out"] = ts
        pos["pnl"] = pos["proceeds"] - pos["stake"] - pos["n_tx"] * f["tx_usd"]
        pos["pnl_pct"] = 100 * pos["pnl"] / pos["stake"]


def apply_bar(pos, ts, o, h, l, c, cfg):
    """Avanza una posicion con una vela (o una foto: o=h=l=c). True si queda cerrada."""
    ex = exit_cfg(cfg, pos.get("exit"))
    p = pos["p_ref"]
    tp = p * ex["tp_mult"]
    stop = p * ex["stop_after_tp_mult"] if pos["tp_done"] else p * (1 - ex["stop_pct"] / 100)
    pos["last_price"], pos["last_check"] = c, ts
    if l <= stop:
        if not pos["tp_done"] and h >= tp:
            pos["ambiguous"] = True      # stop y x2 en la misma vela: cuenta como stop
        _sell(pos, pos["frac_left"], min(stop, o), ex["stop_extra_slippage_pct"], cfg, ts,
              "stop_tras_objetivo" if pos["tp_done"] else "stop")
        return True
    if not pos["tp_done"] and h >= tp:
        pos["tp_done"] = True
        _sell(pos, min(ex["tp_fraction"], pos["frac_left"]), tp, 0.0, cfg, ts, "objetivo")
        if pos["status"] == "cerrada":
            return True
    if ts - pos["t_in"] >= ex["max_hours"] * 3600:
        _sell(pos, pos["frac_left"], c, 0.0, cfg, ts, "objetivo_y_tiempo" if pos["tp_done"] else "tiempo")
        return True
    return False


def wants(strat, s, w, cfg, t):
    r = cfg["strategies"][strat]
    age = (t - s["created"]) / 60.0
    if not (r["age_min"][0] <= age <= r["age_min"][1]):
        return False
    if strat == "control":
        if w.get("source") not in r["sources"]:
            return False
        return int(hashlib.sha256(w["mint"].encode()).hexdigest(), 16) % int(r["one_in"]) == 0
    if s["mc"] < r["mc_min"] or s["mc"] > r.get("mc_max", float("inf")) or s["liq"] < r["liq_min"]:
        return False
    if r.get("need_x") and not (s["x"] or w.get("x")):
        return False
    if r.get("need_tg") and not (s["tg"] or w.get("tg")):
        return False
    if "chg_h1_min" in r:
        ratio = s["buys_h1"] / max(s["sells_h1"], 1.0)
        if (s["chg_h1"] <= r["chg_h1_min"] or s["chg_m5"] < r["chg_m5_min"]
                or ratio < r["buy_sell_h1_min"] or s["buys_h1"] + s["sells_h1"] < r["txns_h1_min"]
                or s["vol_h1"] < r["vol_h1_min"] or s["mc"] < r["mc_vs_max_min"] * w.get("max_mc", 0)):
            return False
    return True


# ---------------------------------------------------------------- estado y pasada

def load(d):
    cfg = CFG
    p = os.path.join(d, "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            cfg = deep_merge(CFG, json.load(f))
    st = {"watch": {}, "positions": [], "ticks": 0, "started": now(), "last_tick": None, "notes": {}}
    p = os.path.join(d, "estado.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            st.update(json.load(f))
    return cfg, st


def save(d, st):
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "estado.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(d, "estado.json"))
    cols = ["strat", "exit", "symbol", "mint", "t_in", "t_out", "mc_in", "reason", "pnl", "pnl_pct", "ambiguous"]
    with open(os.path.join(d, "operaciones.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in st["positions"]:
            if p["status"] == "cerrada":
                w.writerow([round(p[c], 4) if isinstance(p.get(c), float) else p.get(c) for c in cols])


def tick(d):
    cfg, st = load(d)
    t = now()
    uni = cfg["universe"]
    watch, positions = st["watch"], st["positions"]

    found = discover_gt() + discover_pump()
    nuevos = 0
    for it in found:
        w = watch.get(it["mint"])
        if w is None:
            if len(watch) >= uni["max_watch"]:
                continue
            watch[it["mint"]] = w = {"mint": it["mint"], "pool": it.get("pool"), "source": it["source"],
                                     "first_seen": t, "x": False, "tg": False, "max_mc": 0.0,
                                     "done": [], "misses": 0}
            nuevos += 1
        w["x"] = w["x"] or bool(it.get("x"))
        w["tg"] = w["tg"] or bool(it.get("tg"))
    st["notes"]["fuentes"] = sorted({it["source"] for it in found})

    abiertas = [p for p in positions if p["status"] == "abierta"]
    mints = set(watch) | {p["mint"] for p in abiertas}
    pools = {m: w.get("pool") for m, w in watch.items()}
    pools.update({p["mint"]: p["pool"] for p in abiertas})
    snap, ok = snapshot(sorted(mints), pools, uni["dex"])
    if not ok and mints:
        log("  sin datos de mercado: no se evalua nada en esta pasada")
        st["last_tick"], st["ticks"] = t, st["ticks"] + 1
        save(d, st)
        return st

    # 1) salidas
    gt_calls, velas, n_velas = 0, {}, 0
    for p in sorted(abiertas, key=lambda q: q["last_check"]):
        s = snap.get(p["mint"])
        if s is None:
            if t - p["last_seen"] > 2 * 3600:   # desaparecida: se da por perdido lo que quedaba
                _sell(p, p["frac_left"], 0.0, 0.0, cfg, t, "sin_datos")
            continue
        p["last_seen"] = t
        closed = False
        if cfg["gt_candles"] and t - p["last_check"] > 180:
            key = (p["pool"], int(p["last_check"]))     # las salidas de una misma entrada comparten velas
            if key not in velas and gt_calls < cfg["gt_max_calls"]:
                gt_calls += 1
                velas[key] = gt_bars(p["pool"], p["mint"], p["last_check"], t)
                n_velas += len(velas[key])
                time.sleep(2.2)
            bars = velas.get(key, [])
            if bars and not (1 / 3 < bars[-1][4] / s["price"] < 3):
                log(f"  velas descartadas para {p['symbol']}: no cuadran con el precio actual")
                bars = []
            for b in bars:
                if apply_bar(p, *b, cfg):
                    closed = True
                    break
        if not closed:
            apply_bar(p, t, s["price"], s["price"], s["price"], s["price"], cfg)

    # 2) entradas
    entradas = 0
    for m, w in list(watch.items()):
        s = snap.get(m)
        if s is None:
            w["misses"] += 1
            if w["misses"] >= 6:
                del watch[m]
            continue
        w["misses"] = 0
        w["pool"], w["max_mc"] = s["pool"], max(w["max_mc"], s["mc"])
        age_h = (t - s["created"]) / 3600
        if age_h > uni["max_pair_age_h"]:
            del watch[m]
            continue
        for strat in cfg["strategies"]:
            if strat in w["done"]:
                continue
            if wants(strat, s, w, cfg, t):
                for ex_name in cfg["exits"]:
                    positions.append(open_position(strat, m, s, cfg, t, ex_name))
                w["done"].append(strat)
                entradas += 1

    st["last_tick"], st["ticks"] = t, st["ticks"] + 1
    st["notes"]["velas"] = gt_calls
    st["notes"]["velas_total"] = st["notes"].get("velas_total", 0) + n_velas
    save(d, st)
    n_ab = sum(1 for p in positions if p["status"] == "abierta")
    log(f"pasada {st['ticks']}: {nuevos} nuevas en vigilancia ({len(watch)} en total), "
        f"{entradas} entradas, {n_ab} abiertas, {len(positions) - n_ab} cerradas"
        + (f", {gt_calls} consultas de velas ({n_velas} velas)" if gt_calls else ""))
    return st


# ---------------------------------------------------------------- informe

def stats(positions, strat, exit_name, cfg):
    mias = [p for p in positions if p["strat"] == strat and p.get("exit", DEFAULT_EXIT) == exit_name]
    cl = [p for p in mias if p["status"] == "cerrada"]
    n = len(cl)
    out = {"n": n, "abiertas": len(mias) - n}
    if not n:
        return dict(out, x2=0, x2_pct=None, media=None, total=0.0, lo=None, hi=None, veredicto="sin datos")
    r = [p["pnl_pct"] for p in cl]
    media = sum(r) / n
    sd = math.sqrt(sum((x - media) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    err = 1.96 * sd / math.sqrt(n)
    x2 = sum(1 for p in cl if p["tp_done"])
    out.update(x2=x2, x2_pct=100 * x2 / n, media=media, total=sum(p["pnl"] for p in cl),
               lo=media - err, hi=media + err, peor=min(r), mejor=max(r))
    if n < cfg["min_trades_verdict"]:
        out["veredicto"] = f"faltan {cfg['min_trades_verdict'] - n} operaciones"
    elif out["lo"] > 0:
        out["veredicto"] = "gana en simulacion"
    elif out["hi"] < 0:
        out["veredicto"] = "pierde"
    else:
        out["veredicto"] = "sin conclusion"
    return out


def breakeven_rate(cfg, exit_name=DEFAULT_EXIT):
    """% de operaciones que deben tocar el objetivo antes del stop para no perder.
    Aproximado: no cuenta las que se cierran por tiempo."""
    f, ex = cfg["fees"], exit_cfg(cfg, exit_name)
    side = (1 - (f["bot_pct"] + f["pool_pct"]) / 100)
    k = side * side * (1 - f["slippage_pct"] / 100) / (1 + f["slippage_pct"] / 100)
    se = 1 - ex["stop_extra_slippage_pct"] / 100
    win = k * (ex["tp_fraction"] * ex["tp_mult"] + (1 - ex["tp_fraction"]) * ex["stop_after_tp_mult"] * se) \
        - 1 - (2 if ex["tp_fraction"] >= 1 else 3) * f["tx_usd"] / cfg["stake_usd"]
    lose = k * (1 - ex["stop_pct"] / 100) * se - 1 - 2 * f["tx_usd"] / cfg["stake_usd"]
    return 100 * (-lose) / (win - lose)


def fmt_t(ts):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo("Europe/Madrid")).strftime("%d/%m %H:%M")
    except Exception:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%d/%m %H:%M UTC")


def pct(v, signo=True):
    return "–" if v is None else (f"{v:+.1f}%" if signo else f"{v:.0f}%")


NOMBRES = {"control": "Control (sin filtro)", "basico": "Filtro básico", "impulso": "Filtro de impulso"}

CSS = """
:root{--bg:#f3f5f4;--card:#fff;--fg:#17201c;--mut:#5d6b65;--line:#d9dfdc;--up:#0b7a53;--down:#b3372c;--acc:#1f5f8b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111614;--card:#1a211e;--fg:#e6ece9;--mut:#93a39c;--line:#2c3632;--up:#4fd1a1;--down:#f08a7e;--acc:#7fb8e0;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#111614;--card:#1a211e;--fg:#e6ece9;--mut:#93a39c;--line:#2c3632;--up:#4fd1a1;--down:#f08a7e;--acc:#7fb8e0;color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding-inline:16px;padding-block:20px 40px;display:grid;gap:20px}
h1{font-size:1.35rem;margin:0}h2{font-size:1rem;margin:0 0 8px}p{margin:0}
.mut{color:var(--mut);font-size:.87rem}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{font-size:.78rem;color:var(--mut);font-weight:600;letter-spacing:.03em;text-transform:uppercase}
.up{color:var(--up)}.down{color:var(--down)}.tag{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:.8rem}
"""


def render(d):
    cfg, st = load(d)
    pos = st["positions"]
    e = html.escape
    filas = ""
    for s in cfg["strategies"]:
        for xn in cfg["exits"]:
            x = stats(pos, s, xn, cfg)
            cls = "" if x["media"] is None else ("up" if x["media"] > 0 else "down")
            rango = "–" if x.get("lo") is None else f"{x['lo']:+.0f}% a {x['hi']:+.0f}%"
            filas += (f"<tr><td>{e(NOMBRES.get(s, s))}</td><td>{e(exit_label(cfg, xn))}</td><td>{x['n']}</td>"
                      f"<td>{x['abiertas']}</td><td>{pct(x['x2_pct'], False)}</td><td>{breakeven_rate(cfg, xn):.0f}%</td>"
                      f"<td class='{cls}'>{pct(x['media'])}</td><td>{rango}</td>"
                      f"<td class='{cls}'>{x['total']:+.2f} $</td><td><span class='tag'>{e(x['veredicto'])}</span></td></tr>")

    def fila_pos(p, cerrada):
        if cerrada:
            cls = "up" if p["pnl"] > 0 else "down"
            fin = f"<td>{e(p['reason'])}{' *' if p['ambiguous'] else ''}</td><td class='{cls}'>{p['pnl_pct']:+.1f}%</td>"
        else:
            mv = 100 * (p["last_price"] / p["p_ref"] - 1)
            fin = f"<td>abierta</td><td class='{'up' if mv > 0 else 'down'}'>{mv:+.1f}%</td>"
        return (f"<tr><td>{e(str(p['symbol']))}</td><td>{e(NOMBRES.get(p['strat'], p['strat']))}</td>"
                f"<td>{e(exit_label(cfg, p.get('exit')))}</td><td>{fmt_t(p['t_in'])}</td>"
                f"<td>{p['mc_in'] / 1000:.0f}k</td>{fin}</tr>")

    ab = sorted((p for p in pos if p["status"] == "abierta"), key=lambda p: -p["t_in"])[:45]
    ce = sorted((p for p in pos if p["status"] == "cerrada"), key=lambda p: -p["t_out"])[:45]
    cab = ("<tr><th>Moneda</th><th>Entrada por</th><th>Salida</th><th>Hora</th><th>Cap.</th>"
           "<th>Estado</th><th>Resultado</th></tr>")
    vacio = "<tr><td colspan='7' class='mut'>Todavía nada.</td></tr>"
    ult = fmt_t(st["last_tick"]) if st.get("last_tick") else "nunca"
    fuentes = ", ".join(st.get("notes", {}).get("fuentes", [])) or "ninguna"
    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper-bot pump.fun</title><style>{CSS}</style></head><body><main>
<header><h1>Paper-bot pump.fun</h1><p class="mut">Dinero simulado. Última pasada: {ult} · {st['ticks']} pasadas · {len(st['watch'])} monedas en vigilancia · fuentes activas: {e(fuentes)}</p></header>
<section class="card"><h2>¿Qué combinación de entrada y salida gana?</h2><div class="scroll"><table>
<tr><th>Entrada por</th><th>Salida</th><th>Cerradas</th><th>Abiertas</th><th>Aciertan</th><th>Necesita</th><th>Media/op.</th><th>Rango 95%</th><th>Total</th><th>Veredicto</th></tr>{filas}</table></div>
<p class="mut" style="margin-top:10px">Salida = objetivo · stop · tiempo máximo; al tocar el objetivo se vende todo. «Aciertan» es el % que toca el objetivo antes del stop; «Necesita» es el mínimo aproximado para no perder con esos costes. El veredicto exige {cfg['min_trades_verdict']} operaciones cerradas. Cada operación simula {cfg['stake_usd']:.0f} $.</p></section>
<section class="card"><h2>Abiertas</h2><div class="scroll"><table>{cab}{''.join(fila_pos(p, False) for p in ab) or vacio}</table></div></section>
<section class="card"><h2>Últimas cerradas</h2><div class="scroll"><table>{cab}{''.join(fila_pos(p, True) for p in ce) or vacio}</table></div>
<p class="mut" style="margin-top:10px">* stop y objetivo en la misma vela: se cuenta como stop.</p></section>
<p class="mut">Simulación: precios reales, ejecución supuesta. En real los stops se ejecutan peor y hay monedas que no dejan vender. Un resultado positivo aquí no garantiza ganar dinero. Con varias combinaciones a la vez, la mejor puede serlo por suerte: hay que confirmarla con monedas nuevas.</p>
</main></body></html>"""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser(description="Paper-bot pump.fun (dinero simulado)")
    ap.add_argument("modo", choices=["tick", "loop", "informe"])
    ap.add_argument("--dir", default="datos")
    ap.add_argument("--cada", type=int, default=60, help="segundos entre pasadas en modo loop")
    ap.add_argument("--minutos", type=float, default=0, help="en modo loop, parar tras N minutos (0 = no parar)")
    a = ap.parse_args()
    fin = time.time() + a.minutos * 60 if a.minutos > 0 else None
    if a.modo == "informe":
        render(a.dir)
        return
    while True:
        try:
            tick(a.dir)
            render(a.dir)
        except Exception as e:
            log(f"error en la pasada: {type(e).__name__}: {e}")
            if a.modo == "tick":
                raise
        if a.modo == "tick" or (fin and time.time() + a.cada > fin):
            return
        time.sleep(max(15, a.cada))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
