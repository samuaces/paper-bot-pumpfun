#!/usr/bin/env python3
"""
paper-bot: elige monedas graduadas de pump.fun el solo y opera con DINERO SIMULADO.

No toca ninguna wallet ni pide claves: solo lee datos publicos y apunta lo que
habria pasado, con comisiones y slippage incluidos. Las cuentas se llevan en euros
(el resultado de cada operacion es su importe por lo que se mueve el precio, asi que
no depende del cambio de divisa); el tamano de las monedas viene en dolares. Sirve para medir si un filtro
selecciona mejor que comprar sin filtro (grupo "control") antes de arriesgar nada.

Como mide:
  - Decide las entradas con la foto de mercado de DexScreener, una vez por minuto.
  - Precio de entrada y todas las salidas salen de una sola fuente: las velas de 1 minuto
    de GeckoTerminal. Asi se ven las mechas dentro de cada minuto y no se pierde nada
    aunque el bot este parado un rato: al volver, reconstruye lo ocurrido.
  - Una vela solo se da por buena cuando tiene 2 minutos de antiguedad.
  - Dentro de un minuto no se sabe en que orden paso cada cosa, asi que se apunta siempre lo peor:
    si la moneda se hunde de golpe y ese minuto cierra por debajo del stop, se da por vendida al
    cierre (un stop no llega a tiempo en un desplome); y si toca el objetivo pero el minuto cierra
    por debajo, se da por vendida al cierre, no al objetivo.

Uso (solo Python 3.9+, sin instalar nada):
    python bot.py tick            una pasada
    python bot.py loop --cada 60  pasadas continuas (--minutos N para parar tras N minutos)
    python bot.py informe         regenera el informe sin consultar nada

Salida en la carpeta --dir (por defecto ./datos): estado.json, operaciones.csv (las ultimas), index.html
y archivo/*.csv (el detalle de las pruebas antiguas, que salen del estado para que no crezca sin fin).
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

import aprende

CFG = {
    "stake_eur": 10.0,                      # importe simulado por operacion, en euros
    "fees": {"bot_pct": 1.0,                # comision del bot, por lado
             "pool_pct": 0.30,              # comision del pool, por lado
             "slippage_pct": 1.0,           # deslizamiento por lado
             "tx_eur": 0.05},               # coste fijo por transaccion, en euros
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
        # compra a ciegas cualquier graduada con el mismo tamano minimo que los filtros, y vuelve a
        # probar la misma moneda cada hora mientras lo cumpla (esas repeticiones solo con las salidas de
        # una hora o menos). Es de donde sale la mayor parte de lo que aprende.
        "control": {"age_min": [15, 1440], "one_in": 1, "repite_min": 60,
                    "repite_max": 2,    # repeticiones por pasada como mucho: asi van saliendo de continuo y no todas de golpe
                    "mc_min": 20000, "mc_max": 2000000, "liq_min": 10000,
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
    # la cuenta: un saldo unico que solo compra lo que el aprendizaje ve con ganancia tras costes.
    # No tantea: de probar cosas se encarga el laboratorio, que no gasta saldo.
    "cuenta": {"activa": True, "saldo_eur": 30.0, "huecos": 3, "min_compra_eur": 3.0,
               "margen_pct": 2.0,       # ganancia esperada minima, ya descontados los costes
               "max_horas": 1.0},       # la cuenta solo usa salidas que cierran en este tiempo como mucho
    "aprende": {"sigma0": 45.0, "tau": 10.0, "tau_bias": 10.0, "tope_pct": 150.0},
    "archiva_h": 2.0,                   # las pruebas cerradas hace mas de estas horas pasan al archivo
    "gt": {"pages_new": 1,        # paginas de pools nuevos cada vez que se consulta
           "new_every": 2,        # se consulta una pasada de cada N: el cupo gratuito es escaso
           "candle_calls": 6,     # tope de monedas a las que se les pide velas en una pasada
           "min_gap_s": 4.0,      # separacion minima entre llamadas; se alarga sola si avisa de exceso
           "max_gap_s": 90.0,
           "tick_budget_s": 40,   # dentro de una pasada, no se empiezan llamadas pasado este tiempo
           "settle_s": 120,       # antiguedad minima de una vela para darla por buena
           "fallback_min": 30},   # moneda sin velas (error que no es de cupo) tanto tiempo: foto de mercado
}

STATE_V = 3     # 3: las ventas se apuntan al peor precio entre su nivel y el cierre del minuto
DEFAULT_EXIT = "x2_24h"
UA = "Mozilla/5.0 (paper-bot; solo lectura)"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/"
PUMP = "https://frontend-api-v3.pump.fun/"
DIAG = {}
_gt = {"last": 0.0, "block": 0.0, "gap": 0.0, "t0": 0.0, "err": ""}


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
    txt = f"x{ex['tp_mult']:g}, stop −{ex['stop_pct']:g} %, " + (f"{h:g} h" if h >= 1 else f"{h * 60:g} min")
    return txt.replace(".", ",")


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


def gt_ready(cfg):
    """Hay cupo y tiempo para otra llamada a GeckoTerminal dentro de esta pasada?"""
    t = time.time()
    gap = max(_gt["gap"], cfg["gt"]["min_gap_s"])
    start = max(t, _gt["block"], _gt["last"] + gap)
    return start - _gt["t0"] <= cfg["gt"]["tick_budget_s"]


def gt_json(path, what, cfg):
    """GeckoTerminal con cupo propio: llamadas espaciadas; si avisa de exceso (429), se frena sola."""
    g = cfg["gt"]
    _gt["err"] = ""
    if not gt_ready(cfg):
        _gt["err"] = "cupo"
        return None
    gap = max(_gt["gap"], g["min_gap_s"])
    wait = max(_gt["block"], _gt["last"] + gap) - time.time()
    if wait > 0:
        time.sleep(wait)
    _gt["last"] = time.time()
    DIAG["gt_calls"] = DIAG.get("gt_calls", 0) + 1
    try:
        d = http_json(GT + path, tries=1)
        _gt["gap"] = max(g["min_gap_s"], gap * 0.9)
        return d
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            _gt["err"] = "cupo"
            _gt["gap"] = min(g["max_gap_s"], gap * 1.6)
            _gt["block"] = time.time() + _gt["gap"]
            DIAG["gt_429"] = DIAG.get("gt_429", 0) + 1
        else:
            _gt["err"] = "fallo"
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
        if not gt_ready(cfg):
            break
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
    None si no se pudo consultar (no es lo mismo que [], que significa "sin operaciones");
    en ese caso _gt["err"] dice si fue por cupo o por un fallo de esa moneda."""
    got, before = {}, int(until)
    for _ in range(3):
        d = gt_json(f"pools/{pool}/ohlcv/minute?aggregate=1&limit=1000"
                    f"&before_timestamp={before}&currency=usd&token={mint}", "GeckoTerminal velas", cfg)
        try:
            rows = d["data"]["attributes"]["ohlcv_list"]
        except (TypeError, KeyError):
            _gt["err"] = _gt["err"] or "fallo"
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


def open_position(strat, mint, s, cfg, t, exit_name=DEFAULT_EXIT, feat=None):
    stake = cfg["stake_eur"]
    return {"feat": feat or {}, "id": f"{strat}/{exit_name}:{mint}:{int(t)}", "strat": strat, "exit": exit_name, "mint": mint,
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
        pos["pnl"] = pos["proceeds"] - pos["stake"] - pos["n_tx"] * f["tx_eur"]
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
        # un stop se ejecuta peor que su nivel. Y si la moneda se hunde de golpe y el minuto cierra aun mas
        # abajo, no habria dado tiempo a vender en el stop: se apunta vendida al cierre de ese minuto
        venta = min(min(stop, o) * (1 - ex["stop_extra_slippage_pct"] / 100), c)
        _sell(pos, pos["frac_left"], venta, 0.0, cfg, ts, "stop_tras_objetivo" if pos["tp_done"] else "stop")
        return True
    if not pos["tp_done"] and h >= tp:
        pos["tp_done"] = True            # toca el objetivo; si el minuto cierra por debajo, se apunta el cierre
        _sell(pos, min(ex["tp_fraction"], pos["frac_left"]), min(tp, c), 0.0, cfg, ts, "objetivo")
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
        if c:                                       # precio de entrada: el peor (mas caro) entre la foto con la que
            if ratios is not None:                  # se decidio y el cierre de ese minuto. Asi una moneda que se hunde
                ratios.append(round(c[4] / pos["p_dex"], 3))    # justo al comprarla cuenta como la perdida que es.
            pos["p_ref"] = max(pos["p_dex"], c[4])
            pos["last_price"] = c[4]
            pos["units"] = _units(pos["stake"], pos["p_ref"], cfg)
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


def corrige_entradas(positions, cfg):
    """Arreglo de operaciones apuntadas antes de la regla del peor precio de entrada: si el precio de
    entrada quedo muy por debajo del de la foto, la moneda se hundio en el minuto de la compra y la
    perdida real fue casi total. Se recalcula una sola vez. Devuelve cuantas ha corregido."""
    n = 0
    for p in positions:
        if (p.get("corregida") or p.get("compacta") or not p.get("rebased") or not p.get("p_dex")
                or p["p_ref"] >= 0.8 * p["p_dex"]):
            continue
        hundido = p["p_ref"]
        p["corregida"], p["p_ref"] = True, p["p_dex"]
        p["units"] = _units(p["stake"], p["p_dex"], cfg)
        if p["status"] == "cerrada":            # se vendio en el stop, pero a precio ya hundido
            ex = exit_cfg(cfg, p.get("exit"))
            p.update(status="abierta", frac_left=1.0, proceeds=0.0, n_tx=1, tp_done=False, events=[])
            _sell(p, 1.0, hundido, ex["stop_extra_slippage_pct"], cfg, p["t_out"], "stop")
        n += 1
    return n


def refresh_exits(positions, snap, cfg, t, notes):
    """Pide velas con el cupo que haya: primero las monedas con algun plazo ya vencido, despues
    las que llevan mas tiempo sin revisar. Lo que no entra hoy se reconstruye en otra pasada."""
    until = (int(t) - cfg["gt"]["settle_s"]) // 60 * 60
    by_pool = {}
    for p in positions:
        if p["status"] == "abierta":
            p.pop("gt_fail", None)                  # marca del metodo anterior
            by_pool.setdefault(p["pool"], []).append(p)

    def vencida(q):
        return until >= q["t_in"] + exit_cfg(cfg, q.get("exit"))["max_hours"] * 3600

    def prioridad(pool):
        ps = by_pool[pool]
        return (0 if any(vencida(q) for q in ps) else 1, min(q["last_check"] for q in ps))

    calls, pendientes, n_velas, ratios = 0, 0, 0, []
    for pool in sorted(by_pool, key=prioridad):
        ps = by_pool[pool]
        since = min(q["last_check"] if q.get("rebased") else int(q["t_in"]) // 60 * 60 for q in ps)
        if until - since < 60:
            continue
        if calls >= cfg["gt"]["candle_calls"] or not gt_ready(cfg):
            pendientes += 1
            continue
        calls += 1
        bars = gt_bars(pool, ps[0]["mint"], since, until, cfg)
        if bars is None:
            if _gt["err"] == "cupo":                # sin cupo: se queda en cola, no es culpa de la moneda
                pendientes += 1
                continue
            for q in ps:                            # la moneda no tiene velas: se reintenta; si dura, foto de mercado
                q.setdefault("gt_err", t)
                s = snap.get(q["mint"])
                if s and t - q["gt_err"] > cfg["gt"]["fallback_min"] * 60:
                    q["sin_velas"], q["rebased"] = True, True
                    apply_bar(q, t, s["price"], s["price"], s["price"], s["price"], cfg)
            continue
        n_velas += len(bars)
        for q in ps:
            q.pop("gt_err", None)
            advance(q, bars, until, cfg, ratios)
    notes["ratios"] = (notes.get("ratios", []) + ratios)[-80:]
    atraso = max((until - (q["t_in"] + exit_cfg(cfg, q.get("exit"))["max_hours"] * 3600)
                  for q in positions if q["status"] == "abierta" and vencida(q)), default=0)
    return calls, pendientes, n_velas, atraso


def coste_pct(cfg):
    """Lo que cuesta comprar y vender sin que el precio se mueva, en % del importe."""
    f = cfg["fees"]
    side = 1 - (f["bot_pct"] + f["pool_pct"]) / 100
    k = side * side * (1 - f["slippage_pct"] / 100) / (1 + f["slippage_pct"] / 100)
    return 100 * (1 - k) + 100 * 2 * f["tx_eur"] / cfg["stake_eur"]


def clave_ej(p):
    """La misma compra vista por dos filtros es un solo ejemplo: misma moneda, mismo instante, misma salida."""
    return (p["mint"], int(p["t_in"]), p.get("exit", DEFAULT_EXIT))


def resultado_ej(p, cfg):
    return max(-100.0, min(cfg["aprende"]["tope_pct"], p["pnl_pct"]))


def ejemplos(positions, cfg):
    """Operaciones de prueba cerradas, como ejemplos para aprender: {salida: [(rasgos, resultado %)]}."""
    filas, vistos = {xn: [] for xn in cfg["exits"]}, set()
    for p in positions:
        xn = p.get("exit", DEFAULT_EXIT)
        if p["status"] != "cerrada" or not p.get("feat") or xn not in filas:
            continue
        k = clave_ej(p)
        if k in vistos:
            continue
        vistos.add(k)
        filas[xn].append((aprende.vector(p["feat"]), resultado_ej(p, cfg)))
    return filas


def modelos(positions, cfg, arch=None):
    """Un modelo por salida, con los ejemplos que siguen en el estado mas los ya archivados (resumidos)."""
    a = cfg["aprende"]
    viejos = ej_archivados(arch, cfg)
    return {xn: aprende.fit(rows, -coste_pct(cfg), a["sigma0"], a["tau"], a["tau_bias"], viejos.get(xn))
            for xn, rows in ejemplos(positions, cfg).items()}


def pnl_cuenta(p, cfg):
    """Resultado en euros de una operacion de la cuenta: la misma operacion de prueba, a su importe."""
    return p["cuenta"] * p["proceeds"] / p["stake"] - p["cuenta"] - p["n_tx"] * cfg["fees"]["tx_eur"]


def cuenta_estado(positions, cfg):
    mias = [p for p in positions if p.get("cuenta")]
    cerr = [p for p in mias if p["status"] == "cerrada"]
    abie = [p for p in mias if p["status"] == "abierta"]
    resultado = sum(pnl_cuenta(p, cfg) for p in cerr)
    saldo = cfg["cuenta"]["saldo_eur"] + resultado
    return {"saldo": saldo, "libre": saldo - sum(p["cuenta"] for p in abie), "resultado": resultado,
            "abiertas": abie, "cerradas": cerr}


def cuenta_opera(senales, positions, cfg, t, notes, arch=None):
    """De las compras de prueba de esta pasada, la cuenta toma las que el aprendizaje ve con ganancia.
    senales: [(filtro, moneda, rasgos, {salida: posicion})]. Devuelve cuantas ha tomado."""
    c = cfg["cuenta"]
    if not c.get("activa") or not senales:
        return 0
    ms = {xn: m for xn, m in modelos(positions, cfg, arch).items()
          if exit_cfg(cfg, xn)["max_hours"] <= c.get("max_horas", 1e9)}
    cands, mejor_esp = [], None
    for strat, mint, feat, por_salida in senales:
        d = aprende.mejor({xn: m for xn, m in ms.items() if xn in por_salida}, feat)
        if d:
            mejor_esp = d[1] if mejor_esp is None else max(mejor_esp, d[1])
        if d and d[1] > c["margen_pct"]:        # solo si lo aprendido da ganancia esperada tras costes
            cands.append((d[1], d[1], d[0], strat, mint, por_salida))
    cands.sort(key=lambda x: -x[0])
    tomadas = 0
    for val, mu, xn, strat, mint, por_salida in cands:
        est = cuenta_estado(positions, cfg)
        if len(est["abiertas"]) >= c["huecos"]:
            break
        if any(q["mint"] == mint for q in est["abiertas"]):
            continue
        importe = min(est["libre"], est["saldo"] / c["huecos"])
        if importe < c["min_compra_eur"]:
            break
        q = por_salida[xn]
        q["cuenta"], q["cuenta_esp"], q["cuenta_sorteo"] = round(importe, 2), round(mu, 1), round(val, 1)
        tomadas += 1
    notes["senales"] = notes.get("senales", 0) + len(senales)
    notes["tomadas"] = notes.get("tomadas", 0) + tomadas
    notes["cuenta_ronda"] = {"t": int(t), "n": len(senales), "buenas": len(cands), "tomadas": tomadas,
                             "mejor": None if mejor_esp is None else round(mejor_esp, 1)}
    return tomadas


def wants(strat, s, w, cfg, t):
    r = cfg["strategies"][strat]
    age = (t - s["created"]) / 60.0
    if not (r["age_min"][0] <= age <= r["age_min"][1]):
        return False
    if strat == "control":              # a ciegas: una de cada N, solo con el mismo tamano minimo que los filtros
        if w.get("source") not in r["sources"]:
            return False
        if int(hashlib.sha256(w["mint"].encode()).hexdigest(), 16) % int(r["one_in"]) != 0:
            return False
    if (s["mc"] < r.get("mc_min", 0) or s["mc"] > r.get("mc_max", float("inf"))
            or s["liq"] < r.get("liq_min", 0)):
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
    st = {"v": STATE_V, "watch": {}, "positions": [], "ticks": 0, "started": now(), "last_tick": None, "notes": {},
          "archivo": {}}
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


SOBRAN = ("events", "units", "last_price", "now_price", "last_seen", "last_check", "frac_left", "pool", "gt_err")


def save(d, st):
    os.makedirs(d, exist_ok=True)
    limite = (st.get("last_tick") or now()) - 3600
    for p in st["positions"]:           # las cerradas hace mas de una hora sueltan lo que ya no se usa
        if p["status"] == "cerrada" and not p.get("compacta") and p.get("t_out", 0) < limite:
            for k in SOBRAN:
                p.pop(k, None)
            p["compacta"] = True
    tmp = os.path.join(d, "estado.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(d, "estado.json"))
    with open(os.path.join(d, "operaciones.csv"), "w", newline="", encoding="utf-8", errors="backslashreplace") as f:
        w = csv.writer(f)
        w.writerow(COLS_OP)
        for p in st["positions"]:
            if p["status"] == "cerrada":
                w.writerow(fila_csv(p, COLS_OP))


COLS_OP = ["strat", "exit", "symbol", "mint", "t_in", "t_out", "mc_in", "p_ref", "reason", "pnl", "pnl_pct",
           "ambiguous", "sin_velas", "cuenta"]
COLS_ARCH = ["id", "strat", "exit", "symbol", "mint", "t_in", "t_out", "mc_in", "p_dex", "p_ref", "reason", "pnl",
             "pnl_pct", "tp_done", "ambiguous", "sin_velas", "corregida"] + ["f_" + k for k, _, _ in aprende.FEATS]


def fila_csv(p, cols):
    out = []
    for c in cols:
        v = p.get("feat", {}).get(c[2:]) if c.startswith("f_") else p.get(c)
        if c in ("t_in", "t_out") and v is not None:
            v = int(v)                              # segundos enteros: con decimales se perdia la hora exacta
        elif isinstance(v, float):
            v = f"{v:.8g}"
        out.append(v)
    return out


def firma_ej(cfg):
    """Con que tramos y que tope se resumieron los ejemplos archivados: si cambian, el resumen ya no vale."""
    return f"{[(k, c) for k, c, _ in aprende.FEATS]}|{cfg['aprende']['tope_pct']}"


def ej_archivados(arch, cfg):
    """Resumenes de los ejemplos archivados, por salida; vacio si se hicieron con otros tramos."""
    arch = arch or {}
    return arch.get("ej", {}) if arch.get("firma") == firma_ej(cfg) else {}


def archiva(d, st, cfg, t):
    """Saca del estado las pruebas del laboratorio cerradas hace tiempo, para que el archivo de estado
    no crezca sin fin. No se pierde nada de lo que cuenta:
      - lo que el bot aprendio de ellas queda resumido en st["archivo"]["ej"] (ver aprende.resumen);
      - sus cifras por regla (cuantas, medias, aciertos) quedan sumadas en st["archivo"]["reglas"];
      - el detalle de cada una se anade a <dir>/archivo/vN-AAAAMMDD-HH.csv (N: metodo de medicion).
    Las operaciones de la cuenta no se archivan nunca. Todo se prepara aparte y solo al final, si nada
    ha fallado, se cambia el estado: un fallo deja las cosas como estaban y se reintenta en otra pasada
    (el detalle puede entonces quedar repetido en el CSV; la columna id lo delata).
    Devuelve cuantas ha archivado."""
    try:
        horas = float(cfg.get("archiva_h") or 0)
        if horas <= 0:
            return 0
        limite = t - horas * 3600
        pos = st["positions"]
        cand = [p for p in pos if p["status"] == "cerrada" and not p.get("cuenta") and p.get("t_out", t) < limite]
        if not cand:
            return 0
        # la misma compra vista por varios filtros cuenta como un solo ejemplo, asi que sus pruebas se
        # archivan juntas: si alguna sigue viva (salvo que sea de la cuenta, que se queda siempre), esperan
        idc = {id(p) for p in cand}
        esperan = {clave_ej(p) for p in pos if id(p) not in idc and not p.get("cuenta")}
        fuera = [p for p in cand if clave_ej(p) not in esperan]
        if not fuera:
            return 0
        idf = {id(p) for p in fuera}
        quedan = [p for p in pos if id(p) not in idf]
        siguen = {clave_ej(p) for p in quedan}      # gemelas de la cuenta: el ejemplo lo sigue aportando ella
        viejo = st.get("archivo") or {}
        firma = firma_ej(cfg)
        if viejo.get("ej") and viejo.get("firma") != firma:
            aviso("los ejemplos archivados se resumieron con otros tramos: se apartan y se empieza un resumen nuevo")
        ej = dict(viejo.get("ej", {})) if viejo.get("firma") == firma else {}
        reglas = {k: dict(v) for k, v in viejo.get("reglas", {}).items()}
        nuevos, vistos = {}, set()
        for p in fuera:
            xn = p.get("exit", DEFAULT_EXIT)
            k = clave_ej(p)
            if p.get("feat") and k not in vistos and k not in siguen:
                vistos.add(k)
                nuevos.setdefault(xn, []).append((aprende.vector(p["feat"]), resultado_ej(p, cfg)))
            r = reglas.setdefault(f"{p['strat']}/{xn}", {"n": 0, "s1": 0.0, "s2": 0.0, "x2": 0, "total": 0.0,
                                                        "peor": p["pnl_pct"], "mejor": p["pnl_pct"]})
            r["n"] += 1
            r["s1"] += p["pnl_pct"]
            r["s2"] += p["pnl_pct"] ** 2
            r["x2"] += 1 if p["tp_done"] else 0
            r["total"] += p["pnl"]
            r["peor"], r["mejor"] = min(r["peor"], p["pnl_pct"]), max(r["mejor"], p["pnl_pct"])
        for xn, rows in nuevos.items():
            ej[xn] = aprende.resumen(rows, ej.get(xn))
        arch = dict(viejo, ej=ej, reglas=reglas, firma=firma, n=viejo.get("n", 0) + len(fuera))
        if viejo.get("ej") and viejo.get("firma") != firma:
            arch["ej_apartado"] = viejo["ej"]
        filas = [fila_csv(p, COLS_ARCH) for p in fuera]
        carpeta = os.path.join(d, "archivo")
        os.makedirs(carpeta, exist_ok=True)
        ruta = os.path.join(carpeta, datetime.fromtimestamp(t, timezone.utc).strftime(f"v{STATE_V}-%Y%m%d-%H") + ".csv")
        nuevo = not os.path.exists(ruta) or os.path.getsize(ruta) == 0
        with open(ruta, "a", newline="", encoding="utf-8", errors="backslashreplace") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(COLS_ARCH)
            w.writerows(filas)
    except Exception as e:              # pase lo que pase, la pasada sigue y el estado queda como estaba
        aviso(f"no se pudieron archivar las pruebas antiguas ({type(e).__name__}: {e}); se reintentara")
        return 0
    st["archivo"] = arch                # a partir de aqui nada puede fallar
    pos[:] = quedan
    return len(fuera)


def tick(d):
    t0 = time.time()
    DIAG.clear()
    cfg, st = load(d)
    _gt["t0"] = t0
    _gt["gap"] = max(_gt["gap"], st["notes"].get("gt_gap", 0.0))
    t = now()
    uni = cfg["universe"]
    watch, positions, notes = st["watch"], st["positions"], st["notes"]

    found = discover_pump()
    if st["ticks"] % cfg["gt"]["new_every"] == 0:
        found += discover_gt(cfg)
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
    notes["fuentes_ts"] = {**notes.get("fuentes_ts", {}), **{it["source"]: int(t) for it in found}}
    fuentes = sorted(k for k, v in notes["fuentes_ts"].items() if v > t - 600)

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
    notes["corregidas"] = notes.get("corregidas", 0) + corrige_entradas(positions, cfg)
    calls, pendientes, n_velas, atraso = refresh_exits(positions, snap, cfg, t, notes)

    # 2) entradas de prueba (el laboratorio) y, de entre ellas, las que toma la cuenta
    entradas, senales, en_espera = 0, [], []

    def prueba(strat, m, s, w, primera):
        feat = aprende.rasgos(s, w, t)
        salidas = [xn for xn in cfg["exits"] if primera or exit_cfg(cfg, xn)["max_hours"] <= 1.0]
        por_salida = {xn: open_position(strat, m, s, cfg, t, xn, feat) for xn in salidas}
        positions.extend(por_salida.values())
        senales.append((strat, m, feat, por_salida))
        if primera:
            w["done"].append(strat)
        w.setdefault("rep_t", {})[strat] = t

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
            for strat, regla in cfg["strategies"].items():
                repite = regla.get("repite_min")
                primera = strat not in w["done"]
                if not primera and not (repite and t - w.get("rep_t", {}).get(strat, 0) >= repite * 60):
                    continue
                if wants(strat, s, w, cfg, t):
                    if primera:
                        prueba(strat, m, s, w, True)
                        entradas += 1
                    else:                           # las repeticiones esperan turno (ver mas abajo)
                        en_espera.append((w.get("rep_t", {}).get(strat, 0), m, strat, s, w))
            if prunable(s, w, cfg, t):
                del watch[m]
        # repeticiones: unas pocas por pasada, primero las que mas llevan esperando. Si muchas monedas
        # tocan a la vez (pasa al arrancar), se reparten en las pasadas siguientes y ya quedan escalonadas
        hechas = {}
        for _, m, strat, s, w in sorted(en_espera, key=lambda x: (x[0], x[1])):
            tope = cfg["strategies"][strat].get("repite_max")
            if m not in watch or (tope and hechas.get(strat, 0) >= tope):
                continue
            hechas[strat] = hechas.get(strat, 0) + 1
            prueba(strat, m, s, w, False)
            entradas += 1
    else:
        aviso("sin foto de mercado: en esta pasada no se evaluan entradas")
    tomadas = cuenta_opera(senales, positions, cfg, t, notes, st.get("archivo"))
    archivadas = archiva(d, st, cfg, t)

    dur = time.time() - t0
    st["last_tick"], st["ticks"] = t, st["ticks"] + 1
    ab = [p for p in positions if p["status"] == "abierta"]
    notes["fuentes"] = fuentes
    notes["velas_total"] = notes.get("velas_total", 0) + n_velas
    notes["ticks_ts"] = (notes.get("ticks_ts", []) + [int(t)])[-180:]
    notes["durs"] = (notes.get("durs", []) + [round(dur, 1)])[-60:]
    notes["avisos"] = (notes.get("avisos", []) + [[int(t), a] for a in DIAG.get("avisos", [])])[-40:]
    notes["gt_gap"] = round(_gt["gap"], 1)
    notes["gt_hist"] = (notes.get("gt_hist", []) + [[DIAG.get("gt_calls", 0), DIAG.get("gt_429", 0)]])[-120:]
    notes["diag"] = {"dur": round(dur, 1), "gt_calls": DIAG.get("gt_calls", 0), "gt_fallos": DIAG.get("gt_fallos", 0),
                     "gt_429": DIAG.get("gt_429", 0), "gt_gap": round(_gt["gap"], 1), "atraso_vencidas_min": round(atraso / 60),
                     "dex_fallos": DIAG.get("dex_fallos", 0), "pump_fallos": DIAG.get("pump_fallos", 0),
                     "velas": n_velas, "pendientes": pendientes,
                     "revisado_hasta": min((p["last_check"] for p in ab), default=None),
                     "vigilancia_llena": bool(DIAG.get("vigilancia_llena"))}
    save(d, st)
    log(f"pasada {st['ticks']} ({dur:.0f} s): {nuevos} nuevas en vigilancia ({len(watch)} en total), "
        f"{entradas} entradas ({tomadas} para la cuenta), {len(ab)} abiertas, "
        f"{len(positions) - len(ab) + st.get('archivo', {}).get('n', 0)} cerradas ({archivadas} pasan al archivo), "
        f"{calls} monedas revisadas con velas ({n_velas} velas), {pendientes} en cola")
    return st


# ---------------------------------------------------------------- informe

def stats(positions, strat, exit_name, cfg, arch=None):
    """Cifras de una regla (forma de entrar + salida): las pruebas que siguen en el estado mas las archivadas."""
    mias = [p for p in positions if p["strat"] == strat and p.get("exit", DEFAULT_EXIT) == exit_name]
    cl = [p for p in mias if p["status"] == "cerrada"]
    a = (arch or {}).get("reglas", {}).get(f"{strat}/{exit_name}") or {"n": 0, "s1": 0.0, "s2": 0.0, "x2": 0, "total": 0.0}
    n = len(cl) + a["n"]
    out = {"n": n, "abiertas": len(mias) - len(cl)}
    if not n:
        return dict(out, x2=0, x2_pct=None, media=None, total=0.0, lo=None, hi=None, veredicto="sin datos")
    r = [p["pnl_pct"] for p in cl]
    media = (sum(r) + a["s1"]) / n
    s2 = sum(x * x for x in r) + a["s2"]            # suma de cuadrados: permite juntar con lo archivado
    sd = math.sqrt(max(s2 - n * media * media, 0.0) / (n - 1)) if n > 1 else 0.0
    err = 1.96 * sd / math.sqrt(n)
    x2 = sum(1 for p in cl if p["tp_done"]) + a["x2"]
    out.update(x2=x2, x2_pct=100 * x2 / n, media=media, total=sum(p["pnl"] for p in cl) + a["total"],
               lo=media - err, hi=media + err, peor=min(r + ([a["peor"]] if a["n"] else [])),
               mejor=max(r + ([a["mejor"]] if a["n"] else [])))
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
        - 1 - (2 if ex["tp_fraction"] >= 1 else 3) * f["tx_eur"] / cfg["stake_eur"]
    lose = k * (1 - ex["stop_pct"] / 100) * se - 1 - 2 * f["tx_eur"] / cfg["stake_eur"]
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
    if diag.get("atraso_vencidas_min", 0) > 45:
        probs.append(f"hay operaciones con el plazo vencido hace {diag['atraso_vencidas_min']} minutos y aún sin cerrar")
    if med is not None and len(ratios) >= 10 and not (0.93 <= med <= 1.07):
        probs.append("las dos fuentes de precios no coinciden")
    if diag.get("vigilancia_llena"):
        probs.append("la lista de vigilancia está llena")
    errores = [x for x in n.get("errores", []) if x[0] > t - 3600]
    if errores:
        probs.append(f"{len(errores)} pasadas han fallado en la última hora ({errores[-1][1][:80]})")
    raros = sum(1 for r in ratios if not (0.8 <= r <= 1.25))
    return {"raros": raros, "pasadas_hora": len(hora), "dur": statistics.median(durs[-15:]) if durs else None,
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


NOMBRES = {"control": "Control, a ciegas", "basico": "Filtro básico", "impulso": "Filtro de impulso"}
SALIDAS = {"x2_24h": "Larga", "x2_1h": "x2 rápida", "rapida_1h": "Rápida", "relampago_15m": "Relámpago"}
MOTIVOS = {"objetivo": "objetivo", "stop": "stop", "tiempo": "fin de plazo", "sin_datos": "desaparecida",
           "stop_tras_objetivo": "stop tras objetivo", "objetivo_y_tiempo": "objetivo y fin de plazo"}

# Cuaderno de operaciones: una sola columna, alineada a la izquierda; lo que manda es la lista de actividad.
CSS = """
:root{--papel:#f5f6f8;--hoja:#ffffff;--tinta:#1b2430;--gris:#66707d;--raya:#dde1e7;--marca:#2f4bd8;--gana:#0e8a5f;--pierde:#c2412d;--aviso:#9a5b00;
--f:"Schibsted Grotesk",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--papel:#12161c;--hoja:#1a2028;--tinta:#e8ecf1;--gris:#98a2af;--raya:#2a323d;--marca:#8ea2ff;--gana:#45c493;--pierde:#f0836f;--aviso:#e0a84a;color-scheme:dark}}
:root[data-theme="dark"]{--papel:#12161c;--hoja:#1a2028;--tinta:#e8ecf1;--gris:#98a2af;--raya:#2a323d;--marca:#8ea2ff;--gana:#45c493;--pierde:#f0836f;--aviso:#e0a84a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);font:16px/1.45 var(--f);-webkit-text-size-adjust:100%}
main{max-width:700px;margin:0 auto;padding-inline:18px;padding-block:22px 48px;display:flex;flex-direction:column;gap:30px}
h1{font-size:1.5rem;line-height:1.15;margin:0;font-weight:700;letter-spacing:-.01em}
h2{font-size:1.05rem;margin:0 0 10px;font-weight:700}
p{margin:0}.g{color:var(--gris)}.s{font-size:.875rem}
.gana{color:var(--gana)}.pierde{color:var(--pierde)}
.cab{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.estado{display:inline-flex;align-items:center;gap:7px;font-size:.875rem;font-weight:500;padding:5px 11px;border-radius:999px;background:var(--hoja);border:1px solid var(--raya)}
.estado i{width:9px;height:9px;border-radius:50%;background:var(--gana)}
.estado.mal i{background:var(--pierde)}.estado.mal{color:var(--pierde)}
.cifras{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:var(--hoja);border:1px solid var(--raya);border-radius:10px}
.cifras div{padding:13px 14px;min-width:0}.cifras div+div{border-left:1px solid var(--raya)}
.cifras b{display:block;font-size:1.45rem;line-height:1.2;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
.cifras span{font-size:.8125rem;color:var(--gris)}
.act{list-style:none;margin:0;padding:0;background:var(--hoja);border:1px solid var(--raya);border-radius:10px}
.act li{display:grid;grid-template-columns:3.1rem minmax(0,1fr) auto;column-gap:10px;align-items:baseline;padding:11px 14px}
.act li+li{border-top:1px solid var(--raya)}
.act time{color:var(--gris);font-size:.875rem;font-variant-numeric:tabular-nums}
.act .q{font-weight:700;overflow-wrap:anywhere}
.act .q em{font-style:normal;font-weight:500;color:var(--gris);margin-right:6px}
.act.sinhora .q em{margin:0}
.act .q em.c{color:var(--marca)}
.act .r{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}
.act .d{grid-column:2/4;color:var(--gris);font-size:.875rem}
.act.sinhora li{grid-template-columns:minmax(0,1fr) auto}.act.sinhora .d{grid-column:1/3}
.act li.vacio{display:block;padding:16px 14px;color:var(--gris)}
.tabla{overflow-x:auto;background:var(--hoja);border:1px solid var(--raya);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:.9rem}
th,td{text-align:right;padding:9px 12px;white-space:nowrap}
tr+tr td{border-top:1px solid var(--raya)}
th{font-size:.8125rem;color:var(--gris);font-weight:500;border-bottom:1px solid var(--raya)}
th:first-child,td:first-child{text-align:left}td:first-child{font-weight:700}
td.t{text-align:left;font-weight:400}
ul.salud{margin:0;padding:14px 14px 14px 32px;background:var(--hoja);border:1px solid var(--raya);border-radius:10px;font-size:.9rem}
ul.salud li+li{margin-top:5px}
details summary{cursor:pointer;font-weight:700;font-size:1.05rem;margin-bottom:10px}
details summary:focus-visible,a:focus-visible{outline:2px solid var(--marca);outline-offset:3px}
@media (max-width:430px){.cifras b{font-size:1.2rem}.act li{grid-template-columns:2.9rem minmax(0,1fr) auto}.act.sinhora li{grid-template-columns:minmax(0,1fr) auto}}
"""


# El panel pide los precios de ahora directamente a DexScreener desde el navegador, cada 15 segundos.
# Si no puede, deja los del ultimo guardado y lo dice.
JS_PANEL = """
(function(){
var el=document.getElementById("latido"),t=+el.dataset.t;
if(t){var m=Math.round((Date.now()/1000-t)/60);
el.querySelector("span").textContent=m<=1?" (hace un minuto)":" (hace "+m+" minutos)";
if(m>25){var e=document.querySelector(".estado");e.className="estado mal";e.lastChild.textContent="Parado desde hace "+m+" minutos";}}
var filas=[].slice.call(document.querySelectorAll("li[data-mint]")),nota=document.getElementById("vivo");
if(!filas.length||!window.fetch)return;
var mints=[],ok=0,fallo=false;
filas.forEach(function(f){if(mints.indexOf(f.dataset.mint)<0)mints.push(f.dataset.mint);});
function fmt(v){return (v>=0?"+":"\u2212")+Math.abs(v).toFixed(1).replace(".",",")+" %";}
function pie(){if(!nota)return;
if(ok){nota.textContent="Precios en vivo, actualizados hace "+Math.round((Date.now()-ok)/1000)+" s.";nota.className="s gana";}
else if(fallo){nota.textContent="No se han podido cargar los precios en vivo; se muestran los del \u00faltimo guardado.";}}
function ronda(){var trozos=[];for(var i=0;i<mints.length;i+=30)trozos.push(mints.slice(i,i+30));
Promise.all(trozos.map(function(x){return fetch("https://api.dexscreener.com/tokens/v1/solana/"+x.join(",")).then(function(r){if(!r.ok)throw 0;return r.json();});}))
.then(function(res){var px={};
res.forEach(function(lista){(lista||[]).forEach(function(par){var v=+par.priceUsd;if(!v)return;px[par.pairAddress]=v;
if(par.dexId==="pumpswap"&&par.baseToken&&!px[par.baseToken.address])px[par.baseToken.address]=v;});});
filas.forEach(function(f){var v=px[f.dataset.pool]||px[f.dataset.mint];if(!v)return;
var c=(v/+f.dataset.pref-1)*100,r=f.querySelector(".r");r.textContent=fmt(c);r.className="r "+(c>0?"gana":c<0?"pierde":"");});
ok=Date.now();fallo=false;pie();})
.catch(function(){fallo=true;pie();});}
ronda();setInterval(ronda,15000);setInterval(pie,1000);
})();
"""


def es(v, dec=1, signo=True):
    if v is None:
        return "–"
    txt = f"{v:+.{dec}f}" if signo else f"{v:.{dec}f}"
    return txt.replace(".", ",").replace("-", "−")


def hhmm(ts):
    return fmt_t(ts)[6:11]


def aprendido(pos, cfg, arch=None):
    """Frases, en llano, sobre lo que el bot ha aprendido hasta ahora."""
    ej = ejemplos(pos, cfg)
    viejos = {xn: r for xn, r in ej_archivados(arch, cfg).items() if xn in ej}
    cuantos = {xn: len(rows) + viejos.get(xn, {}).get("n", 0) for xn, rows in ej.items()}
    total = sum(cuantos.values())
    if not total:
        return ["Todavía no ha cerrado ninguna operación de prueba de la que aprender. Hasta entonces la cuenta "
                "no compra: parte de que, sin ventaja, cada operación pierde lo que cuestan las comisiones "
                f"(un {es(coste_pct(cfg), 1, False)} %)."]
    out = [f"Ha aprendido de {total} operaciones de prueba cerradas."]
    for xn, rows in ej.items():
        if cuantos[xn]:
            media = (sum(y for _, y in rows) + viejos.get(xn, {}).get("sy", 0.0)) / cuantos[xn]
            out.append(f"Salida {SALIDAS.get(xn, xn).lower()}: {cuantos[xn]} ejemplos, resultado medio {es(media)} %.")
    xn = max(ej, key=lambda k: cuantos[k])
    if cuantos[xn] < 30:
        out.append("Con menos de 30 ejemplos por salida aún no distingue qué monedas van mejor; sigue probando.")
        return out
    a = cfg["aprende"]
    m = aprende.fit(ej[xn], -coste_pct(cfg), a["sigma0"], a["tau"], a["tau_bias"], viejos.get(xn))
    fuertes = sorted(aprende.efectos(m), key=lambda x: -abs(x[1]) / x[2])[:4]
    for nombre, efecto, sd in fuertes:
        if abs(efecto) < 1:
            continue
        fiable = "parece fiable" if abs(efecto) > 2 * sd else "aún poco fiable"
        out.append(f"Con la salida {SALIDAS.get(xn, xn).lower()}, las monedas {nombre} salen "
                   f"{es(abs(efecto), 0, False)} puntos {'mejor' if efecto > 0 else 'peor'} que la media ({fiable}).")
    return out


def render(d):
    cfg, st = load(d)
    pos = st["positions"]
    e = html.escape
    t = now()
    f = cfg["fees"]
    nom_s = lambda p: SALIDAS.get(p.get("exit", DEFAULT_EXIT), p.get("exit", DEFAULT_EXIT))
    color = lambda v: "gana" if v > 0 else "pierde" if v < 0 else ""

    # --- la cuenta
    ct = cuenta_estado(pos, cfg)
    c0 = cfg["cuenta"]["saldo_eur"]
    ev = []
    for p in ct["abiertas"] + ct["cerradas"]:
        prueba = p.get("cuenta_esp", 0) <= cfg["cuenta"]["margen_pct"]
        ev.append((p["t_in"], 0, f"<li><time>{hhmm(p['t_in'])}</time><span class='q'><em class='c'>Compra</em>{e(str(p['symbol']))}</span>"
                   f"<span class='r'>{es(p['cuenta'], 2, False)} €</span><span class='d'>Salida {e(nom_s(p).lower())}: "
                   f"{e(exit_label(cfg, p.get('exit')))}. "
                   + ("Compra de tanteo, para aprender." if prueba else
                      f"Lo aprendido le da un resultado esperado de {es(p.get('cuenta_esp', 0))} %.") + "</span></li>"))
        if p["status"] == "cerrada":
            g = pnl_cuenta(p, cfg)
            ev.append((p["t_out"], 1, f"<li><time>{hhmm(p['t_out'])}</time><span class='q'><em>Vende</em>{e(str(p['symbol']))}</span>"
                       f"<span class='r {color(g)}'>{es(g, 2)} €</span><span class='d'>Por {e(MOTIVOS.get(p['reason'], p['reason']))}, "
                       f"con la salida {e(nom_s(p).lower())}: {es(100 * g / p['cuenta'])} % sobre {es(p['cuenta'], 2, False)} €.</span></li>"))
    ev.sort(key=lambda x: (-x[0], -x[1]))
    act_cuenta = "".join(x[2] for x in ev[:40]) or ("<li class='vacio'>La cuenta todavía no ha comprado nada. Compra cuando, entre las "
                                                    "monedas que el bot prueba, lo aprendido apunta a ganancia después de costes.</li>")
    cartera = ""
    for p in sorted(ct["abiertas"], key=lambda p: -p["t_in"]):
        mv = 100 * (p.get("now_price", p["last_price"]) / p["p_ref"] - 1)
        fin = p["t_in"] + exit_cfg(cfg, p.get("exit"))["max_hours"] * 3600
        cartera += (f"<li data-mint='{e(p['mint'])}' data-pool='{e(str(p.get('pool', '')))}' data-pref='{p['p_ref']:.12g}'>"
                    f"<span class='q'>{e(str(p['symbol']))} <em>{es(p['cuenta'], 2, False)} €</em></span>"
                    f"<span class='r {color(mv)}'>{es(mv)} %</span><span class='d'>Comprada a las {hhmm(p['t_in'])}. "
                    f"Salida {e(nom_s(p).lower())}: {e(exit_label(cfg, p.get('exit')))}; como tarde se vende a las {hhmm(fin)}.</span></li>")
    cartera = cartera or "<li class='vacio'>Nada en cartera ahora mismo.</li>"
    ganadas = sum(1 for p in ct["cerradas"] if pnl_cuenta(p, cfg) > 0)
    ro, ronda = st["notes"].get("cuenta_ronda"), ""
    if ro:                              # por que compro o no la ultima vez que tuvo monedas que valorar
        cuantas = f"{ro['n']} moneda{'s' if ro['n'] != 1 else ''}"
        if ro["tomadas"]:
            ronda = f"A las {hhmm(ro['t'])} valoró {cuantas} y compró {ro['tomadas']}."
        elif ro["buenas"]:
            ronda = (f"A las {hhmm(ro['t'])} valoró {cuantas} y {ro['buenas']} apuntaba{'n' if ro['buenas'] != 1 else ''} a ganancia, "
                     f"pero no compró: las {cfg['cuenta']['huecos']} compras de la cuenta estaban ocupadas o no quedaba saldo libre.")
        elif ro["mejor"] is not None:
            ronda = (f"A las {hhmm(ro['t'])} valoró {cuantas}: a la mejor, lo aprendido le daba un resultado esperado de "
                     f"{es(ro['mejor'])} %. La cuenta solo compra por encima de {es(cfg['cuenta']['margen_pct'], 0)} %, así que no compró.")

    # --- monedas en prueba ahora (laboratorio), una linea por moneda
    en_prueba, vivas = {}, ""
    for p in pos:
        if p["status"] == "abierta":
            en_prueba.setdefault(p["mint"], []).append(p)
    for mint, ps in sorted(en_prueba.items(), key=lambda kv: -max(q["t_in"] for q in kv[1]))[:20]:
        p = min(ps, key=lambda q: q["t_in"])
        mv = 100 * (p.get("now_price", p["last_price"]) / p["p_ref"] - 1)
        vivas += (f"<li data-mint='{e(mint)}' data-pool='{e(str(p.get('pool', '')))}' data-pref='{p['p_ref']:.12g}'>"
                  f"<span class='q'>{e(str(p['symbol']))}</span><span class='r {color(mv)}'>{es(mv)} %</span>"
                  f"<span class='d'>En prueba desde las {hhmm(p['t_in'])}. Tamaño al entrar {p['mc_in'] / 1000:.0f}k $.</span></li>")
    vivas = vivas or "<li class='vacio'>Ahora mismo no hay ninguna moneda en prueba.</li>"

    # --- laboratorio: actividad (una linea por compra y una por grupo de ventas iguales)
    lab, vistas, ventas, hechas = [], {}, {}, set()
    for p in pos:
        k = (p["strat"], p["mint"], int(p["t_in"]))
        vistas[k] = vistas.get(k, 0) + 1
    for p in pos:
        k = (p["strat"], p["mint"], int(p["t_in"]))
        if k not in hechas:
            hechas.add(k)
            lab.append((p["t_in"], 0, f"<li><time>{hhmm(p['t_in'])}</time><span class='q'><em class='c'>Prueba</em>{e(str(p['symbol']))}</span>"
                        f"<span class='r'></span><span class='d'>{e(NOMBRES.get(p['strat'], p['strat']))}. "
                        f"Tamaño {p['mc_in'] / 1000:.0f}k $. {vistas[k]} salida{'s' if vistas[k] != 1 else ''} a la vez.</span></li>"))
        if p["status"] == "cerrada":
            ventas.setdefault((p["strat"], p["mint"], int(p["t_in"]), int(p["t_out"]) // 60, p["reason"]), []).append(p)
    for ps in ventas.values():
        p = ps[0]
        igual = max(q["pnl_pct"] for q in ps) - min(q["pnl_pct"] for q in ps) < 0.05
        media = sum(q["pnl_pct"] for q in ps) / len(ps)
        det = "; ".join(f"{nom_s(q).lower()} {es(q['pnl_pct'])} %" for q in ps)
        nota = (" Mismo minuto que el objetivo; cuenta como stop." if any(q["ambiguous"] for q in ps) else "") + \
               (" Cerrada con la foto de mercado, sin histórico." if any(q.get("sin_velas") for q in ps) else "")
        lab.append((p["t_out"], 1, f"<li><time>{hhmm(p['t_out'])}</time><span class='q'><em>Cierra</em>{e(str(p['symbol']))}</span>"
                    f"<span class='r {color(media)}'>{es(media) + ' %' if igual else ''}</span><span class='d'>Por "
                    f"{e(MOTIVOS.get(p['reason'], p['reason']))}. Salida{'s' if len(ps) > 1 else ''}: {e(det)}. "
                    f"{e(NOMBRES.get(p['strat'], p['strat']))}.{nota}</span></li>"))
    lab.sort(key=lambda x: (-x[0], -x[1]))
    act_lab = "".join(x[2] for x in lab[:30]) or "<li class='vacio'>Todavía no hay pruebas.</li>"

    # --- laboratorio: comparacion de reglas
    filas = ""
    for s in cfg["strategies"]:
        for xn in cfg["exits"]:
            x = stats(pos, s, xn, cfg, st.get("archivo"))
            c = "" if x["media"] is None else color(x["media"])
            if x["n"]:
                det = (f"{x['n']} cerrada{'s' if x['n'] != 1 else ''}, aciertan {x['x2_pct']:.0f} % "
                       f"(necesita {breakeven_rate(cfg, xn):.0f} %). {e(x['veredicto'].capitalize())}.")
            else:
                det = f"Sin operaciones cerradas todavía (necesita {breakeven_rate(cfg, xn):.0f} % de aciertos)."
            filas += (f"<li><span class='q'>{e(NOMBRES.get(s, s))} <em>con salida {e(SALIDAS.get(xn, xn).lower())}</em></span>"
                      f"<span class='r {c}'>{es(x['media']) + ' %' if x['n'] else '–'}</span>"
                      f"<span class='d'>{e(exit_label(cfg, xn))}. {det}</span></li>")
    n_lab_ab = sum(1 for p in pos if p["status"] == "abierta")
    n_lab_ce = len(pos) - n_lab_ab + st.get("archivo", {}).get("n", 0)

    # --- salud
    hl = health(st, t)
    lineas = [f"Pasadas en la última hora: {hl['pasadas_hora']}. Lo normal son unas 50."]
    if hl["dur"] is not None:
        lineas.append(f"Cada pasada tarda unos {hl['dur']:.0f} segundos.")
    if hl["revisado_hasta"]:
        lineas.append(f"Precios revisados minuto a minuto: la operación más atrasada, hasta las {hhmm(hl['revisado_hasta'])}. "
                      f"Lo que va con retraso se reconstruye después sin perder nada.")
    if hl["ratio_med"] is not None:
        lineas.append(f"Las dos fuentes de precios coinciden: diferencia típica de {es(abs(hl['ratio_med'] - 1) * 100, 1, False)} % "
                      f"en {hl['ratio_n']} compras comprobadas.")
    if hl["raros"]:
        lineas.append(f"En {hl['raros']} compra{'s' if hl['raros'] != 1 else ''} el precio cambió de golpe en el mismo minuto; "
                      f"se apunta siempre el peor precio de entrada.")
    lineas.append(f"Avisos en la última hora: {hl['avisos_hora']}."
                  + (" Últimos: " + "; ".join(e(a[:90]) for a in hl["ultimos_avisos"]) + "." if hl["ultimos_avisos"] else ""))
    for pb in hl["problemas"]:
        lineas.append(f"<strong class='pierde'>Problema: {e(pb)}.</strong>")
    estado = ("<span class='estado mal'><i></i>Hay algo que revisar</span>" if hl["problemas"]
              else "<span class='estado'><i></i>Funcionando</span>")
    ult = f"Última actualización a las {hhmm(st['last_tick'])}" if st.get("last_tick") else "Aún no ha hecho ninguna pasada"
    h1 = t - 3600
    n_sen = len({(p["strat"], p["mint"], int(p["t_in"])) for p in pos if p["t_in"] > h1})
    n_com = sum(1 for p in pos if p.get("cuenta") and p["t_in"] > h1)
    n_ven = sum(1 for p in ct["cerradas"] if p["t_out"] > h1)
    ahora = (f"Vigila {len(st['watch'])} monedas. En la última hora ha probado {n_sen} compra{'s' if n_sen != 1 else ''} en el laboratorio; "
             f"la cuenta ha hecho {n_com} compra{'s' if n_com != 1 else ''} y {n_ven} venta{'s' if n_ven != 1 else ''}.")

    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="120">
<title>Paper-bot pump.fun</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;700&display=swap">
<style>{CSS}</style></head><body><main>
<header><div class="cab"><h1>Paper-bot pump.fun</h1>{estado}</div>
<p class="g s" style="margin-top:6px" id="latido" data-t="{int(st.get('last_tick') or 0)}">Dinero simulado. {ult}<span></span>; la página se renueva sola y los datos cambian cada 10 minutos.</p>
<p class="s" style="margin-top:6px">{ahora}</p></header>
<section class="cifras" aria-label="La cuenta">
<div><b>{es(ct['saldo'], 2, False)} €</b><span>saldo; empezó con {es(c0, 0, False)} €</span></div>
<div><b class="{color(ct['resultado'])}">{es(ct['resultado'], 2)} €</b><span>{len(ct['cerradas'])} venta{'s' if len(ct['cerradas']) != 1 else ''}, {ganadas} con ganancia</span></div>
<div><b>{len(ct['abiertas'])} de {cfg['cuenta']['huecos']}</b><span>compras en cartera</span></div></section>
<section><h2>Operaciones de la cuenta</h2><ul class="act">{act_cuenta}</ul>
<p class="g s" style="margin-top:8px">{ronda}</p></section>
<section><h2>En cartera ahora</h2><ul class="act sinhora">{cartera}</ul>
<p class="g s" style="margin-top:8px" id="vivo">Precios del último guardado; cargando los de ahora…</p>
<p class="g s">El porcentaje es cuánto se ha movido el precio desde la compra, sin descontar costes.</p></section>
<section><h2>Monedas en prueba ahora</h2><ul class="act sinhora">{vivas}</ul>
<p class="g s" style="margin-top:8px">Las {len(en_prueba)} monedas que el bot tiene compradas de mentira en el laboratorio (se muestran las 20 últimas), con su precio de ahora frente al de entrada.</p></section>
<section><h2>Lo que va aprendiendo</h2><ul class="salud g">{''.join(f'<li>{e(x)}</li>' for x in aprendido(pos, cfg, st.get("archivo")))}</ul></section>
<section><details><summary>Laboratorio: las pruebas con las que aprende</summary>
<p class="g s" style="margin-bottom:10px">Aparte de la cuenta, el bot prueba muchas más monedas con {es(cfg['stake_eur'], 0, False)} € de mentira cada una y todas las salidas a la vez. De ahí salen los ejemplos de los que aprende. Lleva {n_lab_ce} pruebas cerradas y {n_lab_ab} abiertas.</p>
<h2>Resultado medio de cada regla</h2><ul class="act sinhora">{filas}</ul>
<p class="g s" style="margin:8px 0 18px">Cada salida es un objetivo, un stop y un tiempo máximo. Acertar es tocar el objetivo antes que el stop.</p>
<h2>Últimas pruebas</h2><ul class="act">{act_lab}</ul></details></section>
<section><h2>Salud del bot</h2><ul class="salud g">{''.join(f'<li>{x}</li>' for x in lineas)}</ul></section>
<p class="g s">Simulación con precios reales y ejecución supuesta. Cuando una moneda se hunde de golpe se apunta vendida al precio ya hundido, no al del stop, porque un stop no llega a tiempo. Aun así, con dinero real hay monedas que no dejan vender, así que un resultado positivo aquí no garantiza ganar. El tamaño de cada moneda se da en dólares, como en pump.fun. Registro iniciado el {fmt_t(st['started'])}.</p>
</main>
<script>{JS_PANEL}</script>
</body></html>"""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8", errors="replace") as f:
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
