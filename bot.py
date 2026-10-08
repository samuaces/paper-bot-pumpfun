#!/usr/bin/env python3
"""
paper-bot: elige monedas graduadas de pump.fun el solo y opera con DINERO SIMULADO.

No toca ninguna wallet ni pide claves: solo lee datos publicos y apunta lo que
habria pasado, con comisiones y slippage incluidos. Sirve para medir si un filtro
selecciona mejor que comprar sin filtro (grupo "control") antes de arriesgar nada.

Como mide:
  - Decide las entradas con la foto de mercado de DexScreener, una vez por minuto.
  - Precio de entrada y todas las salidas salen de una sola fuente: las velas de 1 minuto
    de GeckoTerminal. Asi se ven las mechas dentro de cada minuto y no se pierde nada
    aunque el bot este parado un rato: al volver, reconstruye lo ocurrido.
  - Una vela solo se da por buena cuando tiene 2 minutos de antiguedad.

Uso (solo Python 3.9+, sin instalar nada):
    python bot.py tick            una pasada
    python bot.py loop --cada 60  pasadas continuas (--minutos N para parar tras N minutos)
    python bot.py informe         regenera el informe sin consultar nada

Salida en la carpeta --dir (por defecto ./datos): estado.json, operaciones.csv, index.html.
Para cambiar reglas, crea <dir>/config.json con las claves de CFG que quieras pisar.
"""
import argparse
import csv
import hashlib
import html
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CFG = {
    "stake_usd": 10.0,                      # importe simulado por operacion
    "fees": {"bot_pct": 1.0,                # comision del bot, por lado
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
              "rapida_1h": {"tp_mult": 1.5, "stop_pct": 20.0, "max_hours": 1.0},
              # entrar y salir muy rapido: objetivo pequeno, stop corto, 15 minutos
              "relampago_15m": {"tp_mult": 1.2, "stop_pct": 10.0, "max_hours": 0.25}},
    "universe": {"dex": "pumpswap", "max_pair_age_h": 24.0, "max_watch": 3000,
                 "prune_after_min": 120},   # pasada esa edad, se deja de vigilar lo que ya no puede cumplir
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
    "gt": {"pages_new": 2,        # paginas de pools nuevos por pasada
           "candle_calls": 5,     # monedas a las que se les pide velas en cada pasada (por turnos)
           "min_gap_s": 3.0,      # separacion entre llamadas, para no pasarse del limite gratuito
           "settle_s": 120,       # antiguedad minima de una vela para darla por buena
           "fallback_min": 30},   # sin velas tanto tiempo: se usa la foto de mercado y se marca
}

STATE_V = 2
DEFAULT_EXIT = "x2_24h"
UA = "Mozilla/5.0 (paper-bot; solo lectura)"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/"
PUMP = "https://frontend-api-v3.pump.fun/"
DIAG = {}
_gt = {"last": 0.0, "block": 0.0}


def now():
    return time.time()


def log(msg):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), msg, flush=True)


def aviso(msg):
    DIAG.setdefault("avisos", []).append(msg)
    log("  aviso: " + msg)


def exit_cfg(cfg, name):
    return {**cfg["exit"], **cfg.get("exits", {}).get(name or DEFAULT_EXIT, {})}


def exit_label(cfg, name):
    ex = exit_cfg(cfg, name)
    h = ex["max_hours"]
    return f"x{ex['tp_mult']:g} · −{ex['stop_pct']:g}% · " + (f"{h:g} h" if h >= 1 else f"{h * 60:g} min")


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


def try_json(url, what, kind, tries=2):
    try:
        return http_json(url, tries=tries)
    except Exception as e:  # una fuente caida no debe parar la pasada
        DIAG[kind + "_fallos"] = DIAG.get(kind + "_fallos", 0) + 1
        aviso(f"{what} no responde ({type(e).__name__}: {e})")
        return None


def gt_json(path, what, cfg):
    """GeckoTerminal con limite propio: llamadas espaciadas y, si avisa de exceso, pausa."""
    t = time.time()
    if t < _gt["block"]:
        DIAG["gt_fallos"] = DIAG.get("gt_fallos", 0) + 1
        return None
    wait = cfg["gt"]["min_gap_s"] - (t - _gt["last"])
    if wait > 0:
        time.sleep(wait)
    _gt["last"] = time.time()
    DIAG["gt_calls"] = DIAG.get("gt_calls", 0) + 1
    try:
        return http_json(GT + path, tries=1)
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            _gt["block"] = time.time() + 45
        DIAG["gt_fallos"] = DIAG.get("gt_fallos", 0) + 1
        aviso(f"{what} no responde ({type(e).__name__}: {e})")
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

def discover_gt(cfg):
    """Pools recien creados en Solana (todas las plataformas); nos quedamos con los de pump."""
    out = []
    for page in range(1, cfg["gt"]["pages_new"] + 1):
        d = gt_json(f"new_pools?page={page}", "GeckoTerminal nuevos pools", cfg)
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
    return out


def _pump_items(d, source):
    out = []
    for c in d if isinstance(d, list) else []:
        if not isinstance(c, dict) or not c.get("complete") or not c.get("mint"):
            continue
        out.append({"mint": c["mint"], "pool": c.get("pump_swap_pool") or c.get("pool_address"),
                    "source": source, "x": bool(c.get("twitter")), "tg": bool(c.get("telegram"))})
    return out


def discover_pump():
    out = []
    d = try_json(PUMP + "coins?offset=0&limit=50&sort=last_trade_timestamp&order=DESC"
                 "&includeNsfw=false&complete=true", "pump.fun recientes", "pump")
    out += _pump_items(d, "pump_recientes")
    d = try_json(PUMP + "coins/currently-live?limit=50&offset=0&includeNsfw=false", "pump.fun en directo", "pump")
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
        d = try_json(DEX + ",".join(chunk), "DexScreener", "dex")
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


def gt_bars(pool, mint, since, until, cfg):
    """Velas de 1 min con inicio >= since y fin <= until, en orden: (inicio, o, h, l, c).
    None si la fuente falla (no es lo mismo que [], que significa "sin operaciones")."""
    got, before = {}, int(until)
    for _ in range(3):
        d = gt_json(f"pools/{pool}/ohlcv/minute?aggregate=1&limit=1000"
                    f"&before_timestamp={before}&currency=usd&token={mint}", "GeckoTerminal velas", cfg)
        try:
            rows = d["data"]["attributes"]["ohlcv_list"]
        except (TypeError, KeyError):
            return None
        oldest = None
        for r in rows:
            try:
                ts, o, h, l, c = int(r[0]), num(r[1]), num(r[2]), num(r[3]), num(r[4])
            except (IndexError, TypeError, ValueError):
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if ts >= since and ts + 60 <= until and min(o, h, l, c) > 0:
                got[ts] = (ts, o, h, l, c)
        if len(rows) < 1000 or oldest is None or oldest <= since:
            break
        before = oldest
    return [got[k] for k in sorted(got)]


# ---------------------------------------------------------------- operaciones simuladas

def _units(stake, price, cfg):
    f = cfg["fees"]
    return stake * (1 - (f["bot_pct"] + f["pool_pct"]) / 100) / (price * (1 + f["slippage_pct"] / 100))


def open_position(strat, mint, s, cfg, t, exit_name=DEFAULT_EXIT):
    stake = cfg["stake_usd"]
    return {"id": f"{strat}/{exit_name}:{mint}", "strat": strat, "exit": exit_name, "mint": mint,
            "pool": s["pool"], "symbol": s["symbol"], "t_in": t, "p_dex": s["price"], "p_ref": s["price"],
            "mc_in": s["mc"], "units": _units(stake, s["price"], cfg), "stake": stake,
            "frac_left": 1.0, "tp_done": False, "proceeds": 0.0, "n_tx": 1, "last_check": t,
            "last_price": s["price"], "now_price": s["price"], "last_seen": t, "rebased": False,
            "status": "abierta", "reason": "", "ambiguous": False, "events": []}


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
    """Avanza una posicion con una vela que termina en ts. True si queda cerrada."""
    ex = exit_cfg(cfg, pos.get("exit"))
    p = pos["p_ref"]
    tp = p * ex["tp_mult"]
    stop = p * ex["stop_after_tp_mult"] if pos["tp_done"] else p * (1 - ex["stop_pct"] / 100)
    pos["last_price"], pos["last_check"] = c, ts
    if l <= stop:
        if not pos["tp_done"] and h >= tp:
            pos["ambiguous"] = True      # stop y objetivo en la misma vela: cuenta como stop
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


def advance(pos, bars, until, cfg, ratios=None):
    """Aplica a una posicion las velas ya firmes (hasta `until`)."""
    if not pos.get("rebased"):
        em = int(pos["t_in"]) // 60 * 60            # minuto en el que se decidio la entrada
        if until < em + 60:
            return
        c = next((b for b in bars if b[0] == em), None)
        if c:                                       # precio de entrada: cierre de ese minuto, misma fuente que las salidas
            if ratios is not None:
                ratios.append(round(c[4] / pos["p_dex"], 3))
            pos["p_ref"] = pos["last_price"] = c[4]
            pos["units"] = _units(pos["stake"], c[4], cfg)
        pos["rebased"] = True
        pos["last_check"] = em + 60
    deadline = pos["t_in"] + exit_cfg(cfg, pos.get("exit"))["max_hours"] * 3600
    for start, o, h, l, c in bars:
        if start < pos["last_check"]:
            continue
        if start >= deadline:                       # no hubo operaciones hasta pasado el plazo
            break
        if apply_bar(pos, start + 60, o, h, l, c, cfg):
            return
    pos["last_check"] = max(pos["last_check"], until)
    if until >= deadline:
        _sell(pos, pos["frac_left"], pos["last_price"], 0.0, cfg, deadline,
              "objetivo_y_tiempo" if pos["tp_done"] else "tiempo")


def refresh_exits(positions, snap, cfg, t, notes):
    """Pide velas por turnos (primero lo que lleva mas tiempo sin revisar) y actualiza salidas."""
    until = (int(t) - cfg["gt"]["settle_s"]) // 60 * 60
    by_pool = {}
    for p in positions:
        if p["status"] == "abierta":
            by_pool.setdefault(p["pool"], []).append(p)
    calls, pendientes, n_velas, ratios = 0, 0, 0, []
    for pool in sorted(by_pool, key=lambda k: min(q["last_check"] for q in by_pool[k])):
        ps = by_pool[pool]
        since = min(q["last_check"] if q.get("rebased") else int(q["t_in"]) // 60 * 60 for q in ps)
        if until - since < 60:
            continue
        if calls >= cfg["gt"]["candle_calls"]:
            pendientes += 1
            continue
        calls += 1
        bars = gt_bars(pool, ps[0]["mint"], since, until, cfg)
        if bars is None:                            # fuente caida: se reintenta; si dura, foto de mercado
            for q in ps:
                q.setdefault("gt_fail", t)
                s = snap.get(q["mint"])
                if s and t - q["gt_fail"] > cfg["gt"]["fallback_min"] * 60:
                    q["sin_velas"], q["rebased"] = True, True
                    apply_bar(q, t, s["price"], s["price"], s["price"], s["price"], cfg)
            continue
        n_velas += len(bars)
        for q in ps:
            q.pop("gt_fail", None)
            advance(q, bars, until, cfg, ratios)
    notes["ratios"] = (notes.get("ratios", []) + ratios)[-80:]
    return calls, pendientes, n_velas


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


def prunable(s, w, cfg, t):
    """Pasada la ventana del control, una moneda hundida ya no puede cumplir ningun filtro."""
    r = cfg["strategies"].get("basico") or {}
    age = (t - s["created"]) / 60.0
    return (age > cfg["universe"]["prune_after_min"]
            and (s["mc"] < 0.5 * r.get("mc_min", 0) or s["liq"] < 0.5 * r.get("liq_min", 0)))


# ---------------------------------------------------------------- estado y pasada

def load(d):
    cfg = CFG
    p = os.path.join(d, "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            cfg = deep_merge(CFG, json.load(f))
    st = {"v": STATE_V, "watch": {}, "positions": [], "ticks": 0, "started": now(), "last_tick": None, "notes": {}}
    p = os.path.join(d, "estado.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("v") == STATE_V:
            st.update(old)
        else:                           # cambio de metodo de medicion: el registro empieza de cero
            st["watch"] = old.get("watch", {})
            for w in st["watch"].values():
                w["done"] = []
    return cfg, st


def save(d, st):
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "estado.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(d, "estado.json"))
    cols = ["strat", "exit", "symbol", "mint", "t_in", "t_out", "mc_in", "p_ref", "reason", "pnl", "pnl_pct",
            "ambiguous", "sin_velas"]
    with open(os.path.join(d, "operaciones.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in st["positions"]:
            if p["status"] == "cerrada":
                w.writerow([f"{p[c]:.6g}" if isinstance(p.get(c), float) else p.get(c) for c in cols])


def tick(d):
    t0 = time.time()
    DIAG.clear()
    cfg, st = load(d)
    t = now()
    uni = cfg["universe"]
    watch, positions, notes = st["watch"], st["positions"], st["notes"]

    found = discover_gt(cfg) + discover_pump()
    nuevos = 0
    for it in found:
        w = watch.get(it["mint"])
        if w is None:
            if len(watch) >= uni["max_watch"]:
                DIAG["vigilancia_llena"] = True
                continue
            watch[it["mint"]] = w = {"mint": it["mint"], "pool": it.get("pool"), "source": it["source"],
                                     "first_seen": t, "x": False, "tg": False, "max_mc": 0.0,
                                     "done": [], "misses": 0}
            nuevos += 1
        w["x"] = w["x"] or bool(it.get("x"))
        w["tg"] = w["tg"] or bool(it.get("tg"))
    fuentes = sorted({it["source"] for it in found})

    abiertas = [p for p in positions if p["status"] == "abierta"]
    mints = set(watch) | {p["mint"] for p in abiertas}
    pools = {m: w.get("pool") for m, w in watch.items()}
    pools.update({p["mint"]: p["pool"] for p in abiertas})
    snap, ok = snapshot(sorted(mints), pools, uni["dex"])

    # 1) salidas: siempre por velas; la foto solo sirve para mostrar el precio de ahora
    for p in abiertas:
        s = snap.get(p["mint"])
        if s:
            p["last_seen"], p["now_price"] = t, s["price"]
        elif ok and t - p["last_seen"] > 2 * 3600:   # desaparecida: se da por perdido lo que quedaba
            _sell(p, p["frac_left"], 0.0, 0.0, cfg, t, "sin_datos")
    calls, pendientes, n_velas = refresh_exits(positions, snap, cfg, t, notes)

    # 2) entradas
    entradas = 0
    if ok:
        for m, w in list(watch.items()):
            s = snap.get(m)
            if s is None:
                w["misses"] += 1
                if w["misses"] >= 6:
                    del watch[m]
                continue
            w["misses"] = 0
            w["pool"], w["max_mc"] = s["pool"], max(w["max_mc"], s["mc"])
            if (t - s["created"]) / 3600 > uni["max_pair_age_h"]:
                del watch[m]
                continue
            for strat in cfg["strategies"]:
                if strat not in w["done"] and wants(strat, s, w, cfg, t):
                    for ex_name in cfg["exits"]:
                        positions.append(open_position(strat, m, s, cfg, t, ex_name))
                    w["done"].append(strat)
                    entradas += 1
            if prunable(s, w, cfg, t):
                del watch[m]
    else:
        aviso("sin foto de mercado: en esta pasada no se evaluan entradas")

    dur = time.time() - t0
    st["last_tick"], st["ticks"] = t, st["ticks"] + 1
    ab = [p for p in positions if p["status"] == "abierta"]
    notes["fuentes"] = fuentes
    notes["velas_total"] = notes.get("velas_total", 0) + n_velas
    notes["ticks_ts"] = (notes.get("ticks_ts", []) + [int(t)])[-180:]
    notes["durs"] = (notes.get("durs", []) + [round(dur, 1)])[-60:]
    notes["avisos"] = (notes.get("avisos", []) + [[int(t), a] for a in DIAG.get("avisos", [])])[-40:]
    notes["diag"] = {"dur": round(dur, 1), "gt_calls": DIAG.get("gt_calls", 0), "gt_fallos": DIAG.get("gt_fallos", 0),
                     "dex_fallos": DIAG.get("dex_fallos", 0), "pump_fallos": DIAG.get("pump_fallos", 0),
                     "velas": n_velas, "pendientes": pendientes,
                     "revisado_hasta": min((p["last_check"] for p in ab), default=None),
                     "vigilancia_llena": bool(DIAG.get("vigilancia_llena"))}
    save(d, st)
    log(f"pasada {st['ticks']} ({dur:.0f} s): {nuevos} nuevas en vigilancia ({len(watch)} en total), "
        f"{entradas} entradas, {len(ab)} abiertas, {len(positions) - len(ab)} cerradas, "
        f"{calls} monedas revisadas con velas ({n_velas} velas), {pendientes} en cola")
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


def health(st, t):
    """Comprobaciones del propio bot, para ver de un vistazo si esta midiendo bien."""
    n = st.get("notes", {})
    ts = n.get("ticks_ts", [])
    durs = n.get("durs", [])
    diag = n.get("diag", {})
    ratios = n.get("ratios", [])
    hora = [x for x in ts if x > t - 3600]
    avisos = [a for a in n.get("avisos", []) if a[0] > t - 3600]
    med = statistics.median(ratios) if ratios else None
    probs = []
    if st.get("last_tick") and t - st["last_tick"] > 900:
        probs.append("lleva más de 15 minutos parado")
    if durs and statistics.median(durs[-15:]) > 75:
        probs.append("las pasadas tardan demasiado")
    if diag.get("revisado_hasta") and st.get("last_tick") and st["last_tick"] - diag["revisado_hasta"] > 3600:
        probs.append("hay operaciones sin revisar desde hace más de una hora")
    if med is not None and len(ratios) >= 10 and not (0.93 <= med <= 1.07):
        probs.append("las dos fuentes de precios no coinciden")
    if diag.get("vigilancia_llena"):
        probs.append("la lista de vigilancia está llena")
    errores = [x for x in n.get("errores", []) if x[0] > t - 3600]
    if errores:
        probs.append(f"{len(errores)} pasadas han fallado en la última hora ({errores[-1][1][:80]})")
    return {"pasadas_hora": len(hora), "dur": statistics.median(durs[-15:]) if durs else None,
            "revisado_hasta": diag.get("revisado_hasta"), "pendientes": diag.get("pendientes", 0),
            "avisos_hora": len(avisos), "ultimos_avisos": [a[1] for a in avisos[-3:]],
            "ratio_med": med, "ratio_n": len(ratios), "problemas": probs}


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
main{max-width:920px;margin:0 auto;padding-inline:16px;padding-block:20px 40px;display:grid;gap:20px}
h1{font-size:1.35rem;margin:0}h2{font-size:1rem;margin:0 0 8px}p{margin:0}
.mut{color:var(--mut);font-size:.87rem}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{font-size:.78rem;color:var(--mut);font-weight:600;letter-spacing:.03em;text-transform:uppercase}
.up{color:var(--up)}.down{color:var(--down)}.tag{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:.8rem}
.ok{border-color:var(--up);color:var(--up)}.mal{border-color:var(--down);color:var(--down)}
ul{margin:6px 0 0;padding-left:18px}
"""


def render(d):
    cfg, st = load(d)
    pos = st["positions"]
    e = html.escape
    t = now()
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
            marca = (" *" if p["ambiguous"] else "") + (" †" if p.get("sin_velas") else "")
            fin = f"<td>{e(p['reason'])}{marca}</td><td class='{cls}'>{p['pnl_pct']:+.1f}%</td>"
        else:
            mv = 100 * (p.get("now_price", p["last_price"]) / p["p_ref"] - 1)
            fin = f"<td>abierta</td><td class='{'up' if mv > 0 else 'down'}'>{mv:+.1f}%</td>"
        return (f"<tr><td>{e(str(p['symbol']))}</td><td>{e(NOMBRES.get(p['strat'], p['strat']))}</td>"
                f"<td>{e(exit_label(cfg, p.get('exit')))}</td><td>{fmt_t(p['t_in'])}</td>"
                f"<td>{p['mc_in'] / 1000:.0f}k</td>{fin}</tr>")

    ab = sorted((p for p in pos if p["status"] == "abierta"), key=lambda p: -p["t_in"])[:45]
    ce = sorted((p for p in pos if p["status"] == "cerrada"), key=lambda p: -p["t_out"])[:45]
    n_ce = sum(1 for p in pos if p["status"] == "cerrada")
    total = sum(p["pnl"] for p in pos if p["status"] == "cerrada")
    cab = ("<tr><th>Moneda</th><th>Entrada por</th><th>Salida</th><th>Hora</th><th>Cap.</th>"
           "<th>Estado</th><th>Resultado</th></tr>")
    vacio = "<tr><td colspan='7' class='mut'>Todavía nada.</td></tr>"
    ult = fmt_t(st["last_tick"]) if st.get("last_tick") else "nunca"
    hl = health(st, t)
    estado = ("<span class='tag mal'>revisar</span>" if hl["problemas"] else "<span class='tag ok'>todo en orden</span>")
    lineas = [f"Pasadas en la última hora: {hl['pasadas_hora']} (lo normal son unas 50)."]
    if hl["dur"] is not None:
        lineas.append(f"Cada pasada tarda unos {hl['dur']:.0f} segundos.")
    if hl["revisado_hasta"]:
        lineas.append(f"Todas las operaciones abiertas están revisadas al menos hasta las {fmt_t(hl['revisado_hasta'])[-5:]}"
                      f" ({hl['pendientes']} monedas esperando turno).")
    if hl["ratio_med"] is not None:
        lineas.append(f"Las dos fuentes de precios coinciden: diferencia típica de {abs(hl['ratio_med'] - 1) * 100:.1f}% "
                      f"en {hl['ratio_n']} entradas comprobadas.")
    lineas.append(f"Avisos en la última hora: {hl['avisos_hora']}."
                  + (" Últimos: " + "; ".join(e(a[:90]) for a in hl["ultimos_avisos"]) if hl["ultimos_avisos"] else ""))
    for pb in hl["problemas"]:
        lineas.append(f"<strong class='down'>Problema: {e(pb)}.</strong>")
    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Paper-bot pump.fun</title><style>{CSS}</style></head><body><main>
<header><h1>Paper-bot pump.fun</h1><p class="mut">Dinero simulado. Última pasada: {ult} · {len(st['watch'])} monedas en vigilancia · registro iniciado el {fmt_t(st['started'])}</p></header>
<section class="card"><h2>Resumen</h2><p>{n_ce} operaciones cerradas, resultado <strong class="{'up' if total > 0 else 'down' if total < 0 else ''}">{total:+.2f} $</strong> · {sum(1 for p in pos if p['status'] == 'abierta')} abiertas · {cfg['stake_usd']:.0f} $ simulados por operación</p></section>
<section class="card"><h2>¿Qué combinación de entrada y salida gana?</h2><div class="scroll"><table>
<tr><th>Entrada por</th><th>Salida</th><th>Cerradas</th><th>Abiertas</th><th>Aciertan</th><th>Necesita</th><th>Media/op.</th><th>Rango 95%</th><th>Total</th><th>Veredicto</th></tr>{filas}</table></div>
<p class="mut" style="margin-top:10px">Salida = objetivo · stop · tiempo máximo; al tocar el objetivo se vende todo. «Aciertan» es el % que toca el objetivo antes del stop; «Necesita» es el mínimo aproximado para no perder con esos costes. El veredicto exige {cfg['min_trades_verdict']} operaciones cerradas.</p></section>
<section class="card"><h2>Salud del bot {estado}</h2><ul class="mut">{''.join(f'<li>{x}</li>' for x in lineas)}</ul></section>
<section class="card"><h2>Abiertas</h2><div class="scroll"><table>{cab}{''.join(fila_pos(p, False) for p in ab) or vacio}</table></div></section>
<section class="card"><h2>Últimas cerradas</h2><div class="scroll"><table>{cab}{''.join(fila_pos(p, True) for p in ce) or vacio}</table></div>
<p class="mut" style="margin-top:10px">* stop y objetivo en el mismo minuto: se cuenta como stop. † cerrada sin velas, con la foto de mercado.</p></section>
<p class="mut">Simulación: precios reales, ejecución supuesta. En real los stops se ejecutan peor y hay monedas que no dejan vender. Un resultado positivo aquí no garantiza ganar dinero. Con varias combinaciones a la vez, la mejor puede serlo por suerte: hay que confirmarla con monedas nuevas.</p>
</main></body></html>"""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser(description="Paper-bot pump.fun (dinero simulado)")
    ap.add_argument("modo", choices=["tick", "loop", "informe"])
    ap.add_argument("--dir", default="datos")
    ap.add_argument("--cada", type=int, default=60, help="segundos entre el inicio de una pasada y la siguiente")
    ap.add_argument("--minutos", type=float, default=0, help="en modo loop, parar tras N minutos (0 = no parar)")
    a = ap.parse_args()
    fin = time.time() + a.minutos * 60 if a.minutos > 0 else None
    if a.modo == "informe":
        render(a.dir)
        return
    while True:
        ini = time.time()
        try:
            tick(a.dir)
            render(a.dir)
        except Exception as e:
            log(f"error en la pasada: {type(e).__name__}: {e}")
            if a.modo == "tick":
                raise
            try:                        # que el fallo quede a la vista en el informe
                _, st = load(a.dir)
                st["notes"]["errores"] = (st["notes"].get("errores", []) + [[int(now()), f"{type(e).__name__}: {e}"[:200]]])[-20:]
                save(a.dir, st)
                render(a.dir)
            except Exception:
                pass
        espera = max(5.0, a.cada - (time.time() - ini))
        if a.modo == "tick" or (fin and time.time() + espera > fin):
            return
        time.sleep(espera)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
