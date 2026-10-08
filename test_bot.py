"""Prueba sin red: respuestas reales grabadas de DexScreener (08/10/2026 ~07:00) y velas puestas a mano."""
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot

T0 = 1791435600.0   # instante de la foto real; es un minuto exacto
B, H, F = ("CZiwA6eLUthZFLvTVEWG9v1K9Pc7p3eiBKRba2G7pump", "Aov3BtQvrGXM5V9oiHu8PKJWeV8a2rFnr24fwWcNpump",
           "2LMiWGyNjGKyUkH2oFYmMxzwLzUY84dHZf6DUUZFpump")
PB, PH, PF = ("7uQNr3qvET7GcVbA4bbpwevfaP4L3sttrJhxDugYgq41", "HGLSAK3nVLzAZPBMiqrkV52cmZhNVrwVQuzpSnCSYAkw",
              "GZwUWL7SyUXV8z8HNbJW3EPLQ8gGD2A4mYhja1NzMwAd")


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
    pair(B, "BOMBERS", PB, 0.00009675, 92728, 26417.06, 1791403127000, 4.2, -12.6, 70, 51, 4653.8),
    pair(H, "HNET", PH, 0.00002830, 27096, 13268.51, 1791419584000, -12.64, -6.33, 68, 66, 10982.19),
    pair(F, "FEEBIE", PF, 0.00003406, 33018, 14643.88, 1791426982000, -0.02, -24.97, 63, 65, 4377.42),
]
M = "MoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMoMopump"   # inventada: cumple el filtro de impulso
N = "NoSocNoSocNoSocNoSocNoSocNoSocNoSocNoSocpump"   # inventada: sin redes
EXTRA = [pair(M, "MOMO", "poolM", 0.0001, 100000, 30000, (T0 - 2 * 3600 + 600) * 1000, 3.0, 25.0, 140, 80, 20000),
         pair(N, "NOSOC", "poolN", 0.0001, 100000, 30000, (T0 - 2 * 3600 + 600) * 1000, 3.0, 25.0, 140, 80, 20000, socials=False)]

market = {p["baseToken"]["address"]: p for p in copy.deepcopy(REAL + EXTRA)}
clock = {"t": T0 + 10}
gt_pools = [{"attributes": {"address": p["pairAddress"]},
             "relationships": {"dex": {"data": {"id": "pumpswap"}},
                               "base_token": {"data": {"id": "solana_" + p["baseToken"]["address"]}}}}
            for p in REAL + EXTRA]
gt_pools.append({"attributes": {"address": "x"}, "relationships": {"dex": {"data": {"id": "raydium"}},
                                                                   "base_token": {"data": {"id": "solana_OTRA"}}}})
candles, gt_caido, gt_pedidos = {}, set(), []


def fake_http(url, tries=3):
    if url.startswith(bot.DEX):
        return [market[m] for m in url[len(bot.DEX):].split(",") if m in market]
    if "new_pools" in url:
        return {"data": gt_pools if "page=1" in url else []}
    if "/ohlcv/" in url:
        pool = url.split("/pools/")[1].split("/")[0]
        gt_pedidos.append(pool)
        if pool in gt_caido:
            raise OSError("caida")
        before = int(url.split("before_timestamp=")[1].split("&")[0])
        return {"data": {"attributes": {"ohlcv_list": [c for c in candles.get(pool, []) if c[0] < before][::-1]}}}
    if "pump.fun" in url:
        raise OSError("sin red")
    raise AssertionError(url)


bot.CFG["exits"] = {"x2_24h": {}}      # el recorrido principal prueba una sola salida
bot.http_json = fake_http
bot.now = lambda: clock["t"]
bot.time.sleep = lambda s: None
d = tempfile.mkdtemp()
with open(os.path.join(d, "config.json"), "w") as f:
    json.dump({"strategies": {"control": {"one_in": 1}}}, f)   # control compra todas, para probarlo
k = 0.987 * 0.99 / 1.01 * 0.987       # comisiones y slippage de ida y vuelta


def pos(st, strat, mint, ex="x2_24h"):
    return next(p for p in st["positions"] if p["id"] == f"{strat}/{ex}:{mint}")


# --- pasada 1: quien entra donde ------------------------------------------------
st = bot.tick(d)
ids = sorted(p["id"] for p in st["positions"])
esperado = sorted([f"basico/x2_24h:{m}" for m in (B, H, F, M)] + [f"impulso/x2_24h:{M}"]
                  + [f"control/x2_24h:{m}" for m in (M, N)])
assert ids == esperado, ids   # reales: pasan basico, no impulso (h1<0); control solo edad 60-120 min
assert "OTRA" not in st["watch"] and not gt_pedidos       # recien entradas: aun no hay velas firmes
print("entradas OK:", len(ids))

# --- velas de los minutos siguientes ---------------------------------------------
pm, ph = 0.0001, 0.00002830
candles["poolM"] = [[T0, pm, pm * 1.05, pm * 0.99, pm * 1.02, 1],              # minuto de la entrada: cierra en 1.02
                    [T0 + 60, pm * 1.02, pm * 2.1, pm * 1.0, pm * 2.0, 1]]     # toca x2 sobre 1.02
candles[PH] = [[T0, ph, ph, ph, ph, 1],
               [T0 + 60, ph * 0.99, ph * 0.99, ph * 0.65, ph * 0.9, 1]]        # mecha a -35% y rebote
gt_caido.add(PF)                                                               # sin velas para FEEBIE
market[H]["priceUsd"] = str(ph * 0.9)       # la foto no ve la mecha: las velas si

clock["t"] = T0 + 250                       # 4 min despues: firmes las velas hasta T0+120
st = bot.tick(d)
m = pos(st, "impulso", M)
assert m["rebased"] and abs(m["p_ref"] - pm * 1.02) < 1e-12 and st["notes"]["ratios"].count(1.02) == 3
assert m["status"] == "cerrada" and m["reason"] == "objetivo" and m["t_out"] == T0 + 120
assert abs(m["pnl"] - (10 * k * 2 - 10 - 0.10)) < 1e-6, m["pnl"]
h = pos(st, "basico", H)
assert h["status"] == "cerrada" and h["reason"] == "stop" and abs(h["pnl"] - (10 * k * 0.70 * 0.95 - 10 - 0.10)) < 1e-6
b = pos(st, "basico", B)
assert b["status"] == "abierta" and b["rebased"] and b["last_check"] == T0 + 120     # sin operaciones: sigue igual
f_ = pos(st, "basico", F)
assert f_["status"] == "abierta" and not f_["rebased"] and "gt_err" in f_             # sin velas: se reintentara
assert sorted(set(gt_pedidos)) == sorted([PB, PH, PF, "poolM", "poolN"])              # una llamada por moneda
print(f"objetivo OK: {m['pnl_pct']:+.1f}%   mecha a stop vista por velas OK: {h['pnl_pct']:+.1f}%")

# --- turnos: con cupo de 2 monedas por pasada, primero las mas atrasadas ------------
gt_pedidos.clear()
bot.CFG["gt"]["candle_calls"] = 2
clock["t"] = T0 + 400
st = bot.tick(d)
assert len(gt_pedidos) == 2 and st["notes"]["diag"]["pendientes"] == 1, (gt_pedidos, st["notes"]["diag"])
bot.CFG["gt"]["candle_calls"] = 6

# --- aviso de exceso (429): se frena, no culpa a la moneda y lo reintenta despues -------
import urllib.error
gt_pedidos.clear()
exceso = {"on": True}
_fake = bot.http_json
def con_exceso(url, tries=3):
    if exceso["on"] and "/ohlcv/" in url:
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
    return _fake(url, tries)
bot.http_json = con_exceso
clock["t"] = T0 + 460
st = bot.tick(d)
b = pos(st, "basico", B)
assert st["notes"]["diag"]["gt_429"] >= 1 and st["notes"]["diag"]["pendientes"] >= 1 and "gt_err" not in b
assert st["notes"]["gt_gap"] > bot.CFG["gt"]["min_gap_s"] and b["last_check"] < T0 + 300
exceso["on"] = False
bot._gt["block"] = 0
clock["t"] = T0 + 520
st = bot.tick(d)
assert pos(st, "basico", B)["last_check"] == T0 + 360 and st["notes"]["diag"]["gt_429"] == 0
bot.http_json = _fake
print("exceso de llamadas OK: frena y recupera")

# --- un dia despues: cierre por tiempo, moneda desaparecida y moneda sin velas ------
clock["t"] = T0 + 24 * 3600 + 400
del market[N]
market[F]["priceUsd"] = str(0.00003406 * 0.5)
candles[PB] = [[T0 + 25 * 3600, 1, 1, 1, 1, 1]]     # una operacion absurda DESPUES del plazo: no debe contar
st = bot.tick(d)
b = pos(st, "basico", B)
assert b["status"] == "cerrada" and b["reason"] == "tiempo" and b["t_out"] == b["t_in"] + 24 * 3600
assert abs(b["pnl"] - (10 * k * 1 - 10 - 0.10)) < 1e-6, b["pnl"]          # sale al ultimo precio conocido: solo costes
n = pos(st, "control", N)
assert n["status"] == "cerrada" and n["reason"] == "sin_datos" and abs(n["pnl"] - (-10 - 0.10)) < 1e-9
f_ = pos(st, "basico", F)
assert f_["status"] == "cerrada" and f_["reason"] == "stop" and f_["sin_velas"]
assert abs(f_["pnl"] - (10 * k * 0.5 * 0.95 - 10 - 0.10)) < 1e-6
assert not any(p["status"] == "abierta" for p in st["positions"])
print(f"tiempo OK: {b['pnl_pct']:+.1f}%   desaparecida OK: {n['pnl_pct']:+.1f}%   sin velas OK: {f_['pnl_pct']:+.1f}%")

# --- reglas de salida, una a una ----------------------------------------------------
snap = {"price": 1.0, "mc": 50000, "pool": "p", "symbol": "T"}
bot.CFG["exits"]["rapida_1h"] = {"tp_mult": 1.5, "stop_pct": 20.0, "max_hours": 1.0}
r1 = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")
assert bot.apply_bar(r1, 60, 1.0, 1.55, 0.95, 1.5, bot.CFG) and r1["reason"] == "objetivo"
assert abs(r1["pnl"] - (10 * k * 1.5 - 10 - 0.10)) < 1e-6
r2 = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")
assert not bot.apply_bar(r2, 60, 1.0, 1.2, 0.85, 1.1, bot.CFG)       # ni objetivo ni stop
assert bot.apply_bar(r2, 3660, 1.1, 1.15, 1.05, 1.1, bot.CFG) and r2["reason"] == "tiempo"
assert abs(r2["pnl"] - (10 * k * 1.1 - 10 - 0.10)) < 1e-6
r3 = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")
assert bot.apply_bar(r3, 60, 1.0, 1.0, 0.79, 0.8, bot.CFG) and r3["reason"] == "stop"
r4 = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")     # stop y objetivo en el mismo minuto
assert bot.apply_bar(r4, 60, 1.0, 1.6, 0.7, 1.0, bot.CFG) and r4["reason"] == "stop" and r4["ambiguous"]
r5 = bot.open_position("basico", "T", snap, bot.CFG, 30, "rapida_1h")    # el plazo vence sin operaciones
bot.advance(r5, [(0, 1, 1, 1, 1.1), (60, 1.1, 1.2, 1.0, 1.2), (7200, 5, 5, 5, 5)], 7320, bot.CFG)
assert r5["reason"] == "tiempo" and r5["t_out"] == 3630 and abs(r5["pnl"] - (10 * k * 1.2 / 1.1 - 10 - 0.10)) < 1e-6
r6 = bot.open_position("basico", "T", snap, bot.CFG, 30, "rapida_1h")    # la vela del minuto de entrada no dispara salidas
bot.advance(r6, [(0, 1, 9, 0.1, 1.0)], 120, bot.CFG)
assert r6["status"] == "abierta" and r6["last_check"] == 120
print(f"salida rapida OK: objetivo {r1['pnl_pct']:+.1f}%, tiempo {r2['pnl_pct']:+.1f}%, stop {r3['pnl_pct']:+.1f}%")
for xn in ("x2_24h", "rapida_1h"):
    print(f"  {bot.exit_label(bot.CFG, xn)}: necesita {bot.breakeven_rate(bot.CFG, xn):.0f}% de aciertos")

# --- un estado del metodo anterior se reinicia; el informe sale ----------------------
bot.render(d)
page = open(os.path.join(d, "index.html"), encoding="utf-8").read()
assert "Filtro de impulso" in page and "MOMO" in page and "Salud del bot" in page
viejo = json.load(open(os.path.join(d, "estado.json")))
del viejo["v"]
json.dump(viejo, open(os.path.join(d, "estado.json"), "w"))
_, st2 = bot.load(d)
assert st2["positions"] == [] and st2["ticks"] == 0 and all(w["done"] == [] for w in st2["watch"].values())
for s in bot.CFG["strategies"]:
    x = bot.stats(st["positions"], s, "x2_24h", bot.CFG)
    print(f"  {s}: {x['n']} cerradas, media {x['media']:+.1f}%, {x['veredicto']}")
shutil.rmtree(d)
print("TODO OK")
