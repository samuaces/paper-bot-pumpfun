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
    json.dump({"strategies": {"control": {"one_in": 1, "age_min": [60, 120], "repite_min": 0}},   # control: una vez por moneda
               "cuenta": {"activa": False},                                        # la cuenta se prueba aparte, mas abajo
               "archiva_h": 0}, f)                                                 # y el archivado tambien
k = 0.987 * 0.99 / 1.01 * 0.987       # comisiones y slippage de ida y vuelta


def pos(st, strat, mint, ex="x2_24h"):
    return next(p for p in st["positions"] if p["id"].startswith(f"{strat}/{ex}:{mint}:"))


# --- pasada 1: quien entra donde ------------------------------------------------
st = bot.tick(d)
ids = sorted(p["id"].rsplit(":", 1)[0] for p in st["positions"])
esperado = sorted([f"basico/x2_24h:{m}" for m in (B, H, F, M)] + [f"impulso/x2_24h:{M}"]
                  + [f"control/x2_24h:{m}" for m in (M, N)])
assert ids == esperado, ids   # reales: pasan basico, no impulso (h1<0); control solo edad 60-120 min
assert all(len(p["feat"]) == 10 for p in st["positions"]) and pos(st, "impulso", M)["feat"]["bs"] == 1.75
assert "OTRA" not in st["watch"] and not gt_pedidos       # recien entradas: aun no hay velas firmes
print("entradas OK:", len(ids))

# --- velas de los minutos siguientes ---------------------------------------------
pm, ph = 0.0001, 0.00002830
candles["poolM"] = [[T0, pm, pm * 1.05, pm * 0.99, pm * 1.02, 1],              # minuto de la entrada: cierra en 1.02
                    [T0 + 60, pm * 1.02, pm * 2.1, pm * 1.0, pm * 2.05, 1]]    # toca x2 sobre 1.02 y cierra por encima
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
r7 = bot.open_position("basico", "T", snap, bot.CFG, 30, "rapida_1h")    # se hunde un 94 % en el minuto de la compra
bot.advance(r7, [(0, 1, 1, 0.05, 0.057), (60, 0.057, 0.06, 0.04, 0.05)], 120, bot.CFG)
assert r7["p_ref"] == 1.0 and r7["reason"] == "stop" and abs(r7["pnl"] - (10 * k * 0.05 - 10 - 0.10)) < 1e-6    # al cierre, aun mas abajo
r8 = bot.open_position("basico", "T", snap, bot.CFG, 30, "rapida_1h")    # sube en el minuto de la compra: entra mas caro
bot.advance(r8, [(0, 1, 1.2, 1, 1.1)], 120, bot.CFG)
assert abs(r8["p_ref"] - 1.1) < 1e-12 and r8["status"] == "abierta"
r9 = bot.open_position("basico", "T", snap, bot.CFG, 30, "rapida_1h")    # apuntada con la regla antigua: se corrige una vez
r9.update(rebased=True, p_ref=0.057, units=bot._units(10, 0.057, bot.CFG))
bot._sell(r9, 1.0, 0.057 * 0.8, 5.0, bot.CFG, 120, "stop")
assert r9["pnl_pct"] > -30 and bot.corrige_entradas([r9], bot.CFG) == 1 and bot.corrige_entradas([r9], bot.CFG) == 0
assert r9["status"] == "cerrada" and abs(r9["pnl"] - (10 * k * 0.057 * 0.95 - 10 - 0.10)) < 1e-6 and r9["t_out"] == 120
# dentro de un minuto no se sabe el orden: se apunta lo peor entre el nivel y el cierre de ese minuto
ra = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")     # se hunde de golpe: el stop no llega a tiempo
assert bot.apply_bar(ra, 60, 1.0, 1.0, 0.02, 0.02, bot.CFG) and ra["reason"] == "stop"
assert abs(ra["pnl"] - (10 * k * 0.02 - 10 - 0.10)) < 1e-6 and ra["pnl_pct"] < -98
rb = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")     # cierra algo por debajo del stop castigado (0,76)
assert bot.apply_bar(rb, 60, 1.0, 1.0, 0.70, 0.74, bot.CFG) and abs(rb["pnl"] - (10 * k * 0.74 - 10 - 0.10)) < 1e-6
rc = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")     # mecha y rebote: se vende en el stop castigado
assert bot.apply_bar(rc, 60, 1.0, 1.0, 0.70, 0.95, bot.CFG) and abs(rc["pnl"] - r3["pnl"]) < 1e-9
rd = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")     # abre ya por debajo del stop y sigue cayendo
assert bot.apply_bar(rd, 60, 0.6, 0.6, 0.3, 0.4, bot.CFG) and abs(rd["pnl"] - (10 * k * 0.4 - 10 - 0.10)) < 1e-6
re_ = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")    # toca el objetivo de pasada y cierra por debajo
assert bot.apply_bar(re_, 60, 1.0, 1.6, 0.95, 1.1, bot.CFG) and re_["reason"] == "objetivo" and re_["tp_done"]
assert abs(re_["pnl"] - (10 * k * 1.1 - 10 - 0.10)) < 1e-6
rf = bot.open_position("basico", "T", snap, bot.CFG, 0, "rapida_1h")     # stop y objetivo en el mismo minuto, y se hunde
assert bot.apply_bar(rf, 60, 1.0, 1.6, 0.1, 0.1, bot.CFG) and rf["ambiguous"] and abs(rf["pnl"] - (10 * k * 0.1 - 10 - 0.10)) < 1e-6
print(f"desplome en un minuto OK: {ra['pnl_pct']:+.1f}% (antes se apuntaba {r3['pnl_pct']:+.1f}%)   objetivo de pasada OK: {re_['pnl_pct']:+.1f}%")
print(f"moneda que se hunde al comprarla OK: {r7['pnl_pct']:+.1f}% (antes se apuntaba {-28.4:+.1f}%)")
print(f"salida rapida OK: objetivo {r1['pnl_pct']:+.1f}%, tiempo {r2['pnl_pct']:+.1f}%, stop {r3['pnl_pct']:+.1f}%")
for xn in ("x2_24h", "rapida_1h"):
    print(f"  {bot.exit_label(bot.CFG, xn)}: necesita {bot.breakeven_rate(bot.CFG, xn):.0f}% de aciertos")

# --- aprendizaje: algebra, creencia de partida y que aprende un patron claro ----------
import random
import aprende
rng = random.Random(7)
n_ = 6
Bm = [[rng.gauss(0, 1) for _ in range(n_)] for _ in range(n_)]
A_ = [[sum(Bm[i][k] * Bm[j][k] for k in range(n_)) + (2.0 if i == j else 0.0) for j in range(n_)] for i in range(n_)]
b_ = [rng.gauss(0, 1) for _ in range(n_)]
L_ = aprende.cholesky(A_)
x_ = aprende.backward(L_, aprende.forward(L_, b_))
assert max(abs(sum(A_[i][j] * x_[j] for j in range(n_)) - b_[i]) for i in range(n_)) < 1e-9

coste = bot.coste_pct(bot.CFG)
m0 = aprende.fit([], -coste)
feat_alta = {"age": 90, "mc": 50000, "liq": 20000, "h1": 10, "m5": 0, "bs": 1.5, "tx": 100, "turn": 0.5, "dd": 0.95, "soc": 1}
feat_baja = dict(feat_alta, bs=0.7)
mu0, sd0 = aprende.predice(m0, aprende.vector(feat_alta))
assert abs(mu0 + coste) < 1e-9 and abs(sd0 - (100 + 10 * 100) ** 0.5) < 1e-6      # sin datos: pierde los costes, muy incierto
tomadas0 = sum(aprende.decide({"a": m0}, feat_alta, f"k{i}")[1] > 2 for i in range(400)) / 400
assert 0.3 < tomadas0 < 0.55                                                     # al principio tantea

filas_ = []
for i in range(600):
    ft = {"age": rng.choice([30, 90, 300, 900]), "mc": rng.choice([25000, 50000, 100000, 300000]),
          "liq": rng.choice([12000, 20000, 40000]), "h1": rng.choice([-30, -10, 10, 50]), "m5": rng.choice([-8, 0, 8]),
          "bs": rng.choice([0.7, 1.0, 1.5]), "tx": rng.choice([30, 100, 300]), "turn": rng.choice([0.1, 0.5, 2]),
          "dd": rng.choice([0.5, 0.8, 0.95]), "soc": rng.choice([0, 1])}
    filas_.append((aprende.vector(ft), (25 if ft["bs"] >= 1.2 else -15) + rng.gauss(0, 20)))
m1 = aprende.fit(filas_, -coste)
def ajuste_ref(rows, prior, sigma0=45.0, tau=10.0):          # el calculo directo, ejemplo a ejemplo
    def solve(sig):
        A = [[0.0] * aprende.DIM for _ in range(aprende.DIM)]
        bb = [0.0] * aprende.DIM
        for i in range(aprende.DIM):
            A[i][i] = 1.0 / tau ** 2
        bb[0] = prior / tau ** 2
        for idx, y in rows:
            for i in idx:
                bb[i] += y / sig ** 2
                for j in idx:
                    A[i][j] += 1.0 / sig ** 2
        L = aprende.cholesky(A)
        return aprende.backward(L, aprende.forward(L, bb))
    mean = solve(sigma0)
    if len(rows) >= 20:
        sig = max(15.0, (sum((y - sum(mean[i] for i in idx)) ** 2 for idx, y in rows) / (len(rows) - 1)) ** 0.5)
        return sig, solve(sig)
    return sigma0, mean
for corte in (0, 10, 19, 20, 250, 600):
    sig_r, mean_r = ajuste_ref(filas_[:corte], -coste)
    for parte in (0, corte // 3, corte):                     # todo a mano, parte resumida, todo resumido
        mx = aprende.fit(filas_[parte:corte], -coste, base=json.loads(json.dumps(aprende.resumen(filas_[:parte]))))
        assert mx["n"] == corte and abs(mx["sigma"] - sig_r) < 1e-9, (corte, parte, mx["sigma"], sig_r)
        assert max(abs(a_ - b_) for a_, b_ in zip(mx["mean"], mean_r)) < 1e-9
assert aprende.fit(filas_, -coste, base={"n": 5, "sy": 0, "yy": 0, "b": [0], "A": [[0]]})["n"] == 600   # un resumen de otra forma no se usa
mu_a, sd_a = aprende.predice(m1, aprende.vector(feat_alta))
mu_b, _ = aprende.predice(m1, aprende.vector(feat_baja))
assert mu_a > 15 and mu_b < -8 and sd_a < 6, (mu_a, mu_b, sd_a)
t_a = sum(aprende.decide({"a": m1}, feat_alta, f"k{i}")[1] > 2 for i in range(300)) / 300
t_b = sum(aprende.decide({"a": m1}, feat_baja, f"k{i}")[1] > 2 for i in range(300)) / 300
assert t_a > 0.97 and t_b < 0.03, (t_a, t_b)                                      # tras aprender: compra lo bueno, evita lo malo
assert aprende.decide({"a": m1}, feat_alta, "igual") == aprende.decide({"a": m1}, feat_alta, "igual")
assert aprende.mejor({"a": m0, "b": m1}, feat_alta)[0] == "b" and aprende.mejor({"a": m0, "b": m1}, feat_baja)[0] == "a"
top = max(aprende.efectos(m1), key=lambda x: abs(x[1]) / x[2])
assert "compras que ventas" in top[0] or "ventas que compras" in top[0]
print(f"aprendizaje OK: sin datos compra el {tomadas0:.0%} de las señales; con 600 ejemplos, {t_a:.0%} de las buenas y {t_b:.0%} de las malas")

# --- la cuenta de 30 euros: huecos, importes y resultado -------------------------------
cfgc = bot.deep_merge(bot.CFG, {"cuenta": {"activa": True, "margen_pct": -1e9}})
ps_ = []
def senal(mint):
    sn = {"price": 1.0, "mc": 50000, "pool": "p" + mint, "symbol": mint}
    por = {xn: bot.open_position("basico", mint, sn, cfgc, 0, xn, feat_alta) for xn in cfgc["exits"]}
    ps_.extend(por.values())
    return ("basico", mint, feat_alta, por)
notas = {}
assert bot.cuenta_opera([senal(x) for x in "ABCDE"] + [senal("A")], ps_, cfgc, 0, notas) == 3      # 3 huecos, 5 señales
mias = [p for p in ps_ if p.get("cuenta")]
assert [p["cuenta"] for p in mias] == [10.0, 10.0, 10.0] and len({p["mint"] for p in mias}) == 3
est = bot.cuenta_estado(ps_, cfgc)
assert abs(est["saldo"] - 30) < 1e-9 and abs(est["libre"]) < 1e-9
assert bot.cuenta_opera([senal("F")], ps_, cfgc, 60, notas) == 0                                    # sin hueco no compra
g1 = mias[0]
bot._sell(g1, 1.0, g1["p_ref"] * exit_tp if (exit_tp := bot.exit_cfg(cfgc, g1["exit"])["tp_mult"]) else 0, 0, cfgc, 600, "objetivo")
assert abs(bot.pnl_cuenta(g1, cfgc) - g1["pnl"]) < 1e-9                                             # a 10 euros, igual que la prueba
est = bot.cuenta_estado(ps_, cfgc)
assert abs(est["saldo"] - (30 + g1["pnl"])) < 1e-9 and abs(est["libre"] - (10 + g1["pnl"])) < 1e-9
assert bot.cuenta_opera([senal("G")], ps_, cfgc, 700, notas) == 1
nueva = [p for p in ps_ if p.get("cuenta") and p["mint"] == "G"][0]
assert abs(nueva["cuenta"] - round(min(est["libre"], est["saldo"] / 3), 2)) < 1e-9                  # reparte el saldo en 3
g2 = mias[1]
bot._sell(g2, 1.0, g2["p_ref"] * 0.7, 5.0, cfgc, 800, "stop")
est = bot.cuenta_estado(ps_, cfgc)
assert abs(est["resultado"] - (g1["pnl"] + g2["pnl"])) < 1e-9 and (notas["senales"], notas["tomadas"]) == (8, 4)
ro_ = notas["cuenta_ronda"]
assert (ro_["t"], ro_["n"], ro_["buenas"], ro_["tomadas"]) == (700, 1, 1, 1) and isinstance(ro_["mejor"], float)
assert all(bot.exit_cfg(cfgc, p["exit"])["max_hours"] <= 1 for p in ps_ if p.get("cuenta"))       # la cuenta solo usa salidas cortas
cfg2 = bot.deep_merge(bot.CFG, {"cuenta": {"activa": True}})                                       # margen normal: +2 %
nt_ = {}
assert bot.cuenta_opera([senal("S")], [], cfg2, 0, nt_) == 0                                        # sin ejemplos no compra
assert nt_["cuenta_ronda"]["buenas"] == 0 and nt_["cuenta_ronda"]["mejor"] == round(-coste, 1)      # y deja dicho por que
hist = []
for i in range(120):                                                                                # 120 ejemplos: bs alto gana, bs bajo pierde
    for pre, ft, y in (("a", feat_alta, 30.0), ("b", feat_baja, -20.0)):
        q = bot.open_position("control", f"{pre}{i}", {"price": 1.0, "mc": 5e4, "pool": "p", "symbol": "h"}, cfg2, i, "rapida_1h", ft)
        q.update(status="cerrada", pnl_pct=y + (i % 7 - 3), pnl=0.0, t_out=i + 1)
        hist.append(q)
def senal2(mint, ft):
    sn = {"price": 1.0, "mc": 50000, "pool": "p" + mint, "symbol": mint}
    por = {xn: bot.open_position("control", mint, sn, cfg2, 999, xn, ft) for xn in cfg2["exits"]}
    hist.extend(por.values())
    return ("control", mint, ft, por)
assert bot.cuenta_opera([senal2("BUENA", feat_alta), senal2("MALA", feat_baja)], hist, cfg2, 999, {}) == 1
elegida = [p for p in hist if p.get("cuenta")]
assert [p["mint"] for p in elegida] == ["BUENA"] and elegida[0]["exit"] == "rapida_1h" and elegida[0]["cuenta_esp"] > 15
cfgn = bot.deep_merge(bot.CFG, {"cuenta": {"activa": True, "margen_pct": 1e9}})
assert bot.cuenta_opera([senal("H")], ps_, cfgn, 900, {}) == 0                                      # si nada supera el margen, no compra
ej = bot.ejemplos(ps_ + [dict(g1, strat="impulso")], cfgc)                                          # misma compra por dos filtros: un ejemplo
assert sum(len(v) for v in ej.values()) == 2
print("la cuenta solo compra con ganancia esperada OK")
print(f"cuenta OK: 3 huecos de 10 €, saldo tras una ganada y una perdida {est['saldo']:.2f} €")

# --- el control vuelve a probar la misma moneda cada hora, solo con salidas cortas ------
d2 = tempfile.mkdtemp()
with open(os.path.join(d2, "config.json"), "w") as f:
    json.dump({"strategies": {"control": {"age_min": [60, 1440]}}, "cuenta": {"activa": False}}, f)
todas = dict(bot.CFG["exits"])
bot.CFG["exits"] = {"x2_24h": {}, "x2_1h": {"max_hours": 1.0}, "rapida_1h": {"tp_mult": 1.5, "stop_pct": 20.0, "max_hours": 1.0}}
market[N] = copy.deepcopy(EXTRA[1])
market[N]["pairCreatedAt"] = (T0 + 86400 - 2 * 3600) * 1000
for m_ in list(market):
    if m_ != N:
        del market[m_]
gt_pools[:] = [g for g in gt_pools if g["attributes"]["address"] == "poolN"]
clock["t"] = T0 + 86400
st3 = bot.tick(d2)
assert sorted(p["exit"] for p in st3["positions"]) == ["rapida_1h", "x2_1h", "x2_24h"]           # primera vez: todas las salidas
clock["t"] += 30 * 60
st3 = bot.tick(d2)
assert len(st3["positions"]) == 3                                                                # a la media hora aun no repite
clock["t"] += 31 * 60
st3 = bot.tick(d2)
nuevas = [p for p in st3["positions"] if p["t_in"] == clock["t"]]
assert sorted(p["exit"] for p in nuevas) == ["rapida_1h", "x2_1h"] and len({p["id"] for p in st3["positions"]}) == 5
clock["t"] += 3 * 3600
st3 = bot.tick(d2)                                                                               # y las cerradas antiguas se compactan
viejas = [p for p in st3["positions"] if p.get("compacta")]
assert viejas and all("events" not in p and "pnl" in p for p in viejas)
# las dos de la primera hora cerraron hace mas de 2 h: han pasado al archivo sin dejar de contar
assert st3["archivo"]["n"] == 2 and len(st3["positions"]) == 5 and len(viejas) == 2      # 1 larga abierta, 2 compactas, 2 recien abiertas
assert sorted(st3["archivo"]["reglas"]) == ["control/rapida_1h", "control/x2_1h"]
for xn in ("x2_1h", "rapida_1h"):
    assert bot.stats(st3["positions"], "control", xn, bot.CFG, st3["archivo"])["n"] == 2
    assert bot.stats(st3["positions"], "control", xn, bot.CFG)["n"] == 1
assert st3["archivo"]["ej"]["x2_1h"]["n"] == 1 and len(bot.ejemplos(st3["positions"], bot.CFG)["x2_1h"]) == 1
csvs = os.listdir(os.path.join(d2, "archivo"))
import csv as _csv
with open(os.path.join(d2, "archivo", csvs[0]), encoding="utf-8") as f:
    filas_arch = list(_csv.DictReader(f))
assert len(csvs) == 1 and len(filas_arch) == 2                                                   # con la hora exacta, sin redondeos
assert all(r["t_in"] == str(int(T0 + 86400)) and r["t_out"] == str(int(T0 + 86400 + 3600)) for r in filas_arch)
with open(os.path.join(d2, "operaciones.csv"), encoding="utf-8") as f:
    assert all(r["t_in"] == str(int(T0 + 86400 + 3660)) for r in _csv.DictReader(f))
_, st3b = bot.load(d2)                                                                           # y el archivo se guarda y se relee
assert st3b["archivo"] == st3["archivo"]
visto_ = {}                                                                                      # la pasada le da a la cuenta lo archivado
_co = bot.cuenta_opera
def espia(*a_, **k_):
    visto_["arch"] = a_[5] if len(a_) > 5 else k_.get("arch")
    return _co(*a_, **k_)
bot.cuenta_opera = espia
clock["t"] += 60
st3c = bot.tick(d2)
bot.cuenta_opera = _co
assert visto_["arch"] and visto_["arch"]["n"] == 2                                               # lo que habia archivado al decidir
assert st3c["archivo"]["n"] == 4 and len(st3c["positions"]) == 3                                 # y en esta pasada salen las otras dos
assert bot.stats(st3c["positions"], "control", "x2_1h", bot.CFG, st3c["archivo"])["n"] == 2
bot.render(d2)
pag2 = open(os.path.join(d2, "index.html"), encoding="utf-8").read()
assert 'id="latido"' in pag2 and "Lleva 4 pruebas cerradas y 3 abiertas" in pag2 and "Ha aprendido de 4 operaciones" in pag2
assert pag2.count("2 cerradas, aciertan") == 2 and "1 cerrada," not in pag2                      # el informe cuenta tambien las archivadas
shutil.rmtree(d2)
bot.CFG["exits"] = todas
print("repeticion del control, compactado y paso al archivo OK")

# --- archivo: sacar las pruebas antiguas del estado no cambia nada de lo que cuenta ------
d3 = tempfile.mkdtemp()
cfga = bot.deep_merge(bot.CFG, {"cuenta": {"activa": True}, "exits": {
    "x2_1h": {"max_hours": 1.0}, "relampago_15m": {"tp_mult": 1.2, "stop_pct": 10.0, "max_hours": 0.25}}})
rng2 = random.Random(11)
AHORA = 9.5 * 3600.0
def prueba(strat, mint, t_in, xn, ft, y, t_out, abierta=False):
    q = bot.open_position(strat, mint, {"price": 1.0, "mc": 5e4, "pool": "p" + mint, "symbol": mint}, cfga, t_in, xn, ft)
    if not abierta:
        q.update(status="cerrada", reason="tiempo", pnl_pct=y, pnl=y / 10, proceeds=10 + y / 10 + 0.10, n_tx=2,
                 tp_done=y > 40, t_out=t_out, frac_left=0.0)
    return q
def rasgos_al_azar():
    return {"age": rng2.choice([30, 90, 300, 900]), "mc": rng2.choice([25000, 50000, 100000, 300000]),
            "liq": rng2.choice([12000, 20000, 40000]), "h1": rng2.choice([-30, -10, 10, 50]), "m5": rng2.choice([-8, 0, 8]),
            "bs": rng2.choice([0.7, 1.0, 1.5]), "tx": rng2.choice([30, 100, 300]), "turn": rng2.choice([0.1, 0.5, 2]),
            "dd": rng2.choice([0.5, 0.8, 0.95]), "soc": rng2.choice([0, 1])}
viejo_ = []
for i in range(400):
    ft, t_in = rasgos_al_azar(), i * 80.0
    for xn in ("x2_1h", "rapida_1h", "relampago_15m"):
        y = max(-99.0, (20 if ft["bs"] >= 1.2 else -15) + rng2.gauss(0, 25)) * (4 if i % 97 == 0 else 1)   # alguna pasa del tope
        t_out = t_in + (900 if xn == "relampago_15m" else 3600)
        viejo_.append(prueba("control", f"m{i}", t_in, xn, ft, y, t_out, abierta=t_out > AHORA))
        if i % 5 == 0:                                   # la misma compra vista tambien por otro filtro
            viejo_.append(prueba("basico", f"m{i}", t_in, xn, ft, y, t_out, abierta=t_out > AHORA))
for i in range(0, 400, 9):                              # pruebas antiguas sin rasgos, y de una salida que ya no existe
    viejo_.append(prueba("control", f"s{i}", i * 80.0 + 1, "x2_1h", {}, -12.0 + i % 5, i * 80.0 + 3601))
    viejo_.append(prueba("control", f"q{i}", i * 80.0 + 2, "quitada", rasgos_al_azar(), 33.0, i * 80.0 + 3602))
de_cuenta = [p for p in viejo_ if p["status"] == "cerrada" and p["exit"] == "rapida_1h" and p["strat"] == "basico"][:6]
for p in de_cuenta:                                      # seis antiguas son de la cuenta (y tienen gemela en control)
    p["cuenta"], p["cuenta_esp"] = 8.0, 5.0
sonda = [rasgos_al_azar() for _ in range(25)]
def foto(st_):
    ms = bot.modelos(st_["positions"], cfga, st_.get("archivo"))
    est_ = bot.cuenta_estado(st_["positions"], cfga)
    return {"pred": [aprende.predice(ms[xn], aprende.vector(ft)) for xn in sorted(ms) for ft in sonda],
            "mejor": [aprende.mejor(ms, ft) for ft in sonda],
            "n_ej": {xn: m["n"] for xn, m in ms.items()},
            "stats": {(s_, xn): bot.stats(st_["positions"], s_, xn, cfga, st_.get("archivo"))
                      for s_ in cfga["strategies"] for xn in cfga["exits"]},
            "frases": bot.aprendido(st_["positions"], cfga, st_.get("archivo")),
            "cuenta": (est_["saldo"], est_["libre"], len(est_["cerradas"]), len(est_["abiertas"])),
            "compra": compraria(st_),
            "cerradas": sum(p["status"] == "cerrada" for p in st_["positions"]) + st_.get("archivo", {}).get("n", 0)}
def compraria(st_):
    """Que compraria la cuenta ante las mismas señales nuevas (sobre una copia, sin tocar el estado)."""
    ps_c = copy.deepcopy([p for p in st_["positions"] if not p.get("cuenta")])
    sen = []
    for i_, ft in enumerate(sonda):
        sn = {"price": 1.0, "mc": 50000, "pool": f"pz{i_}", "symbol": f"z{i_}"}
        por = {xn: bot.open_position("control", f"z{i_}", sn, cfga, AHORA, xn, ft) for xn in cfga["exits"]}
        ps_c.extend(por.values())
        sen.append(("control", f"z{i_}", ft, por))
    bot.cuenta_opera(sen, ps_c, cfga, AHORA, {}, st_.get("archivo"))
    return sorted((p["mint"], p["exit"], p["cuenta"], p["cuenta_esp"]) for p in ps_c if p.get("cuenta"))
def iguales(a, b, tol=1e-6):
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(iguales(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(iguales(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))
    return a == b
sta = {"positions": copy.deepcopy(viejo_), "archivo": {}, "last_tick": AHORA, "notes": {}}
antes = foto(sta)
n_antes = len(sta["positions"])
assert antes["n_ej"]["rapida_1h"] > 300 and antes["stats"][("basico", "x2_1h")]["n"] > 50 and antes["cuenta"][2] == 6
assert len(antes["compra"]) == 3                                                 # hay señales que la cuenta compra
# 1) si no se puede escribir el detalle, no se toca nada
open(os.path.join(d3, "archivo"), "w").close()
bot.DIAG.clear()
assert bot.archiva(d3, sta, cfga, AHORA) == 0 and len(sta["positions"]) == n_antes and sta["archivo"] == {}
assert "archivar las pruebas antiguas" in bot.DIAG["avisos"][0]
os.remove(os.path.join(d3, "archivo"))
rara = copy.deepcopy(sta)                                                        # tampoco un dato estropeado: ni rompe ni deja nada a medias
next(p for p in rara["positions"] if p["status"] == "cerrada" and not p.get("cuenta"))["pnl_pct"] = None
assert bot.archiva(d3, rara, cfga, AHORA) == 0 and len(rara["positions"]) == n_antes and rara["archivo"] == {}
assert not os.path.exists(os.path.join(d3, "archivo"))
# 2) por tandas, como hara el bot: primero lo cerrado hace mas de 6 h, luego mas de 4 h, luego el limite normal de 2 h
total_arch = 0
for h_ in (6.0, 4.0, 2.0):
    total_arch += bot.archiva(d3, sta, bot.deep_merge(cfga, {"archiva_h": h_}), AHORA)
    sta = json.loads(json.dumps(sta))                    # entre pasada y pasada el estado va a disco y vuelve
    assert iguales(foto(sta), antes), h_
assert total_arch > 600 and len(sta["positions"]) == n_antes - total_arch and sta["archivo"]["n"] == total_arch
sin_arch = dict(sta, archivo={})                                                 # sin lo archivado saldria otra cosa: la prueba mira algo
assert not iguales(foto(sin_arch)["pred"], antes["pred"]) and foto(sin_arch)["n_ej"] != antes["n_ej"]
assert "quitada" not in bot.modelos(sta["positions"], cfga, sta["archivo"]) and "control/quitada" in sta["archivo"]["reglas"]
otra = bot.deep_merge(cfga, {"aprende": {"tope_pct": 99.0}})                     # resumido con otro tope: no se mezcla
assert bot.modelos(sta["positions"], otra, sta["archivo"])["rapida_1h"]["n"] == len(bot.ejemplos(sta["positions"], otra)["rapida_1h"])
assert bot.archiva(d3, sta, cfga, AHORA) == 0                                    # no queda nada que archivar
quedan_ = sta["positions"]
assert sum(1 for p in quedan_ if p.get("cuenta")) == 6                           # la cuenta nunca se archiva
assert all(p["status"] == "abierta" or p.get("cuenta") or p["t_out"] >= AHORA - 2 * 3600 for p in quedan_)
assert any(p["status"] == "abierta" for p in quedan_)
# 3) de un tiron da lo mismo que por tandas, y el detalle queda en el CSV con la hora exacta
stb = {"positions": copy.deepcopy(viejo_), "archivo": {}}
d4 = tempfile.mkdtemp()
assert bot.archiva(d4, stb, cfga, AHORA) == total_arch and iguales(foto(stb), antes)
assert iguales(stb["archivo"], sta["archivo"])
import csv as _csv
with open(os.path.join(d4, "archivo", os.listdir(os.path.join(d4, "archivo"))[0]), encoding="utf-8") as f:
    det = list(_csv.DictReader(f))
ids_fuera = {p["id"] for p in viejo_} - {p["id"] for p in stb["positions"]}
assert len(det) == total_arch and {r["id"] for r in det} == ids_fuera
r0 = next(r for r in det if r["id"] == viejo_[0]["id"])
assert r0["t_in"] == "0" and r0["t_out"] == "3600" and float(r0["f_bs"]) == viejo_[0]["feat"]["bs"]
assert len({r["id"] for r in det}) == len(det)
assert abs(float(r0["pnl_pct"]) - viejo_[0]["pnl_pct"]) < 1e-5
# 4) con el archivado apagado no hace nada
assert bot.archiva(d4, {"positions": copy.deepcopy(viejo_)}, bot.deep_merge(cfga, {"archiva_h": 0}), AHORA) == 0
# 5) el estado deja de crecer: lo que queda no depende de cuantas pruebas antiguas hubo
assert len(json.dumps(sta["archivo"])) < 40000
shutil.rmtree(d3)
shutil.rmtree(d4)
print(f"archivo OK: {total_arch} pruebas antiguas fuera del estado; aprendizaje, cifras y cuenta, idénticos")

# --- muchas monedas a repetir a la vez: salen de dos en dos, la que mas espera primero -----
d5 = tempfile.mkdtemp()
with open(os.path.join(d5, "config.json"), "w") as f:
    json.dump({"strategies": {"control": {"age_min": [60, 1440]}}, "cuenta": {"activa": False}}, f)
guard_m, guard_p = dict(market), list(gt_pools)
market.clear()
gt_pools[:] = []
t5 = T0 + 3 * 86400
for i in range(5):
    mm = f"Rep{i}RepRepRepRepRepRepRepRepRepRepRepRepRpump"
    market[mm] = pair(mm, f"R{i}", f"poolR{i}", 0.0001, 100000, 30000, (t5 - 2 * 3600) * 1000, 3.0, 25.0, 140, 80, 20000, socials=False)
    gt_pools.append({"attributes": {"address": f"poolR{i}"}, "relationships": {
        "dex": {"data": {"id": "pumpswap"}}, "base_token": {"data": {"id": "solana_" + mm}}}})
clock["t"] = t5
st5 = bot.tick(d5)
assert len({p["mint"] for p in st5["positions"]}) == 5                       # al arrancar, las cinco a la vez
por_pasada = []
for paso in (61, 62, 63, 64):
    clock["t"] = t5 + paso * 60
    st5 = bot.tick(d5)
    por_pasada.append(sorted(p["symbol"] for p in st5["positions"] if p["t_in"] == clock["t"] and p["exit"] == "rapida_1h"))
assert [len(x) for x in por_pasada] == [2, 2, 1, 0] and sorted(sum(por_pasada, [])) == ["R0", "R1", "R2", "R3", "R4"]
clock["t"] = t5 + 122 * 60                                                   # una hora despues ya van escalonadas
st5 = bot.tick(d5)
assert len({p["mint"] for p in st5["positions"] if p["t_in"] == clock["t"]}) == 2
assert sorted(p["symbol"] for p in st5["positions"] if p["t_in"] == clock["t"] and p["exit"] == "rapida_1h") == por_pasada[0]
bot.render(d5)
pag5 = open(os.path.join(d5, "index.html"), encoding="utf-8").read()
assert "valoró" not in pag5                                                  # cuenta apagada: no dice nada de rondas
shutil.rmtree(d5)
market.clear()
market.update(guard_m)
gt_pools[:] = guard_p
print("repeticiones repartidas OK:", por_pasada)

# --- un estado del metodo anterior se reinicia; el informe sale ----------------------
bot.render(d)
page = open(os.path.join(d, "index.html"), encoding="utf-8").read()
assert "Filtro de impulso" in page and "MOMO" in page and "Salud del bot" in page
assert "Operaciones de la cuenta" in page and "Lo que va aprendiendo" in page and "30 €" in page
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
