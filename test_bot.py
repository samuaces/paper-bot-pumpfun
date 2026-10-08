"""Prueba sin red: respuestas reales grabadas de DexScreener (08/10/2026 ~07:00) + precios movidos a mano."""
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import bot

T0 = 1791435600.0   # instante de la foto real
B, H, F = ("CZiwA6eLUthZFLvTVEWG9v1K9Pc7p3eiBKRba2G7pump", "Aov3BtQvrGXM5V9oiHu8PKJWeV8a2rFnr24fwWcNpump",
           "2LMiWGyNjGKyUkH2oFYmMxzwLzUY84dHZf6DUUZFpump")


def pair(mint, sym, pool, price, mc, liq, created, m5, h1, buys, sells, vol, socials=True):
    p = {"chainId": "solana", "dexId": "pumpswap", "pairAddress": pool,
         "baseToken": {"address": mint, "symbol": sym}, "priceUsd": str(price),
         "txns": {"h1": {"buys": buys, "sells": sells}}, "volume": {"h1": vol},
         "priceChange": {"m5": m5, "h1": h1}, "liquidity": {"usd": liq}, "fdv": mc, "marketCap": mc,
         "pairCreatedAt": created}
    if socials:
        p["info"] = {"socials": [{"type": "twitter", "url": "x"}, {"type": "telegram", "url": "t"}]}
    return p


REAL = [  # valores tal cual los devolvio la API
    pair(B, "BOMBERS", "7uQNr3qvET7GcVbA4bbpwevfaP4L3sttrJhxDugYgq41", 0.00009675, 92728, 26417.06, 1791403127000, 4.2, -12.6, 70, 51, 4653.8),
    pair(H, "HNET", "HGLSAK3nVLzAZPBMiqrkV52cmZhNVrwVQuzpSnCSYAkw", 0.00002830, 27096, 13268.51, 1791419584000, -12.64, -6.33, 68, 66, 10982.19),
    pair(F, "FEEBIE", "GZwUWL7SyUXV8z8HNbJW3EPLQ8gGD2A4mYhja1NzMwAd", 0.00003406, 33018, 14643.88, 1791426982000, -0.02, -24.97, 63, 65, 4377.42),
]
# una moneda inventada que si cumple el filtro de impulso, y otra sin redes
M = "MoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMopump"
N = "NoSocNoSocNoSocNoSocNoSocNoSocNoSocNoSocpump"
EXTRA = [pair(M, "MOMO", "poolM", 0.0001, 100000, 30000, (T0 - 2 * 3600) * 1000, 3.0, 25.0, 140, 80, 20000),
         pair(N, "NOSOC", "poolN", 0.0001, 100000, 30000, (T0 - 2 * 3600) * 1000, 3.0, 25.0, 140, 80, 20000, socials=False)]

market = {p["baseToken"]["address"]: p for p in copy.deepcopy(REAL + EXTRA)}
clock = {"t": T0}
gt_pools = [{"attributes": {"address": p["pairAddress"]},
             "relationships": {"dex": {"data": {"id": "pumpswap"}},
                               "base_token": {"data": {"id": "solana_" + p["baseToken"]["address"]}}}}
            for p in REAL + EXTRA]
gt_pools.append({"attributes": {"address": "x"}, "relationships": {"dex": {"data": {"id": "raydium"}},
                                                                   "base_token": {"data": {"id": "solana_OTRA"}}}})
candles = {}


def fake_http(url, tries=3):
    if url.startswith(bot.DEX):
        return [market[m] for m in url[len(bot.DEX):].split(",") if m in market]
    if "new_pools" in url:
        return {"data": gt_pools if "page=1" in url else []}
    if "/ohlcv/" in url:
        pool = url.split("/pools/")[1].split("/")[0]
        return {"data": {"attributes": {"ohlcv_list": candles.get(pool, [])}}}
    if "pump.fun" in url:
        raise OSError("sin red")
    raise AssertionError(url)


bot.http_json = fake_http
bot.now = lambda: clock["t"]
bot.time.sleep = lambda s: None
d = tempfile.mkdtemp()
with open(os.path.join(d, "config.json"), "w") as f:
    json.dump({"strategies": {"control": {"one_in": 1}}, "exit": {"tp_fraction": 0.5}}, f)   # control compra todas; venta parcial, para probar ese camino


def setp(mint, price):
    market[mint]["priceUsd"] = str(price)


def pos(st, strat, mint):
    return next(p for p in st["positions"] if p["id"] == f"{strat}:{mint}")


# --- pasada 1: quien entra donde ------------------------------------------------
st = bot.tick(d)
ids = sorted(p["id"] for p in st["positions"])
esperado = sorted([f"basico:{m}" for m in (B, H, F, M)] + [f"impulso:{M}"] + [f"control:{m}" for m in (M, N)])
assert ids == esperado, ids   # reales: pasan basico, no impulso (h1<0); control solo edad 60-120 min
assert "OTRA" not in st["watch"]
print("entradas OK:", len(ids))

# --- pasada 2 (1 min despues): MOMO hace x2, HNET cae 35% -------------------------
clock["t"] += 60
setp(M, 0.00021)
setp(H, 0.00002830 * 0.65)
st = bot.tick(d)
m = pos(st, "impulso", M)
assert m["tp_done"] and m["status"] == "abierta" and abs(m["frac_left"] - 0.5) < 1e-9
h = pos(st, "basico", H)
k = 0.987 * 0.99 / 1.01 * 0.987       # comisiones y slippage de ida y vuelta
exp_h = 10 * k * 0.65 * 0.95 - 10 - 2 * 0.05   # se ejecuta al precio visto (peor que el nivel), -5% extra
assert h["status"] == "cerrada" and h["reason"] == "stop" and abs(h["pnl"] - exp_h) < 1e-6, (h["pnl"], exp_h)
print(f"stop OK: {h['pnl_pct']:+.1f}%")

# --- pasada 3: MOMO vuelve al precio de entrada -> stop del resto en entrada -------
clock["t"] += 60
setp(M, 0.0001)
st = bot.tick(d)
m = pos(st, "impulso", M)
exp_m = 10 * k * (0.5 * 2 + 0.5 * 1 * 0.95) - 10 - 3 * 0.05
assert m["status"] == "cerrada" and m["reason"] == "stop_tras_x2" and abs(m["pnl"] - exp_m) < 1e-6, (m["pnl"], exp_m)
print(f"x2 + stop en entrada OK: {m['pnl_pct']:+.1f}%")

# --- pasada 4 (10 min despues, modo cron): velas de 1 min revelan una mecha ---------
# BOMBERS: entre pasadas toco -32% y se recupero; la foto final no lo ve, las velas si.
p0 = 0.00009675
t_prev = clock["t"]
clock["t"] += 600
base = int(t_prev) // 60 * 60 + 60
candles["7uQNr3qvET7GcVbA4bbpwevfaP4L3sttrJhxDugYgq41"] = [   # mas reciente primero, como la API
    [base + 180, p0 * 0.9, p0 * 1.0, p0 * 0.9, p0 * 1.0, 1],
    [base + 120, p0 * 0.8, p0 * 0.9, p0 * 0.68, p0 * 0.9, 1],
    [base + 60, p0, p0, p0 * 0.8, p0 * 0.8, 1],
]
setp(B, p0)
st = bot.tick(d)
b = pos(st, "basico", B)
exp_b = 10 * k * 0.70 * 0.95 - 10 - 2 * 0.05   # abre por encima del stop: se ejecuta en el nivel, -5% extra
assert b["status"] == "cerrada" and b["reason"] == "stop" and abs(b["pnl"] - exp_b) < 1e-6, (b["pnl"], exp_b)
print(f"mecha detectada por velas OK: {b['pnl_pct']:+.1f}%")

# --- cierre por tiempo y moneda desaparecida ---------------------------------------
clock["t"] = T0 + 24 * 3600 + 5
del market[N]
bot.CFG["gt_candles"] = False
st = bot.tick(d)
f_ = pos(st, "basico", F)
assert f_["status"] == "cerrada" and f_["reason"] == "tiempo"
n = pos(st, "control", N)
assert n["status"] == "cerrada" and n["reason"] == "sin_datos" and abs(n["pnl"] - (-10 - 2 * 0.05)) < 1e-9
assert not any(p["status"] == "abierta" for p in st["positions"])

# --- regla por defecto: al tocar el objetivo se vende todo ---------------------------
snap = {"price": 1.0, "mc": 50000, "pool": "p", "symbol": "T"}
q = bot.open_position("basico", "T", snap, bot.CFG, 0)
assert bot.apply_bar(q, 60, 1.0, 2.3, 0.9, 2.2, bot.CFG)          # vela que toca x2
exp_q = 10 * k * 2 - 10 - 2 * 0.05
assert q["status"] == "cerrada" and q["reason"] == "x2" and abs(q["pnl"] - exp_q) < 1e-6, (q["pnl"], exp_q)
print(f"salida total en el objetivo OK: {q['pnl_pct']:+.1f}%")

bot.render(d)
page = open(os.path.join(d, "index.html"), encoding="utf-8").read()
assert "Filtro de impulso" in page and "MOMO" in page
be = bot.breakeven_x2_rate(bot.CFG)
print(f"umbral para no perder: {be:.1f}% de operaciones deben tocar x2")
for s in bot.CFG["strategies"]:
    x = bot.stats(st["positions"], s, bot.CFG)
    print(f"  {s}: {x['n']} cerradas, media {x['media']:+.1f}%, {x['veredicto']}")
shutil.rmtree(d)
print("TODO OK")
