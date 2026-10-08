"""
Aprendizaje del paper-bot.

Cada operacion de prueba que se cierra es un ejemplo: "una moneda con estos rasgos, con esta
salida, acabo en +X % o -X % despues de costes". Con esos ejemplos se ajusta, para cada tipo de
salida, un modelo sencillo (regresion lineal bayesiana) que estima el resultado esperado de una
compra nueva segun sus rasgos, junto con cuanta incertidumbre tiene esa estimacion.

La exploracion (probar de todo para tener ejemplos) la hace el laboratorio, que no gasta saldo.
La cuenta solo aprovecha: compra cuando el resultado esperado supera el margen pedido incluso
despues de restarle su margen de error, y elige la salida que mejor queda asi. Parte de la creencia
de que, sin ventaja, una operacion pierde lo que cuestan las comisiones, asi que sin ejemplos no compra.

Por que restar el margen de error: la cuenta valora cientos de monedas, y una estimacion imprecisa
sale positiva por pura casualidad en unas cuantas. Comprar "lo que salga positivo" es comprar esas
casualidades (paso el primer dia: 15 ventas, media -9 %). Exigir que siga positiva tras restar el
margen de error deja fuera la casualidad.

Solo usa la biblioteca estandar de Python.
"""
import hashlib
import math
import random

# (clave, cortes, descripcion de cada tramo). Un valor cae en el tramo numero "cuantos cortes supera".
FEATS = [
    ("age", [60, 180, 600], ["con menos de 1 hora de vida", "con entre 1 y 3 horas de vida",
                             "con entre 3 y 10 horas de vida", "con más de 10 horas de vida"]),
    ("mc", [35000, 75000, 200000], ["de menos de 35.000 $ de tamaño", "de entre 35.000 y 75.000 $ de tamaño",
                                    "de entre 75.000 y 200.000 $ de tamaño", "de más de 200.000 $ de tamaño"]),
    ("liq", [15000, 30000], ["con poca liquidez", "con liquidez media", "con mucha liquidez"]),
    ("h1", [-20, 0, 30], ["que caen más de un 20 % en la última hora", "que caen algo en la última hora",
                          "que suben hasta un 30 % en la última hora", "que suben más de un 30 % en la última hora"]),
    ("m5", [-5, 5], ["que caen en los últimos 5 minutos", "planas en los últimos 5 minutos",
                     "que suben en los últimos 5 minutos"]),
    ("bs", [0.9, 1.2], ["con más ventas que compras", "con compras y ventas igualadas", "con más compras que ventas"]),
    ("tx", [50, 200], ["con poca actividad", "con actividad media", "con mucha actividad"]),
    ("turn", [0.3, 1.0], ["con poco volumen para su liquidez", "con volumen medio para su liquidez",
                          "con mucho volumen para su liquidez"]),
    ("dd", [0.6, 0.9], ["lejos de su máximo", "algo por debajo de su máximo", "cerca de su máximo"]),
    ("soc", [0.5], ["sin X y Telegram", "con X y Telegram"]),
]
DIM = 1 + sum(len(c) + 1 for _, c, _ in FEATS)


def rasgos(s, w, t):
    """Rasgos de una moneda en el momento de la compra, a partir de la foto de mercado."""
    return {
        "age": round((t - s["created"]) / 60.0, 1), "mc": round(s["mc"]), "liq": round(s["liq"]),
        "h1": round(s["chg_h1"], 1), "m5": round(s["chg_m5"], 1),
        "bs": round(s["buys_h1"] / max(s["sells_h1"], 1.0), 2), "tx": round(s["buys_h1"] + s["sells_h1"]),
        "turn": round(s["vol_h1"] / max(s["liq"], 1.0), 2),
        "dd": round(s["mc"] / max(w.get("max_mc", 0.0), s["mc"], 1.0), 2),
        "soc": 1 if (s["x"] or w.get("x")) and (s["tg"] or w.get("tg")) else 0,
    }


def vector(feat):
    """Posiciones activas del vector de rasgos: la 0 (comun a todas) y un tramo por rasgo."""
    idx, base = [0], 1
    for key, cuts, _ in FEATS:
        v = feat.get(key, 0.0)
        idx.append(base + sum(1 for c in cuts if v >= c))
        base += len(cuts) + 1
    return idx


def nombres():
    out = ["cualquier moneda"]
    for _, _, labels in FEATS:
        out += labels
    return out


# ---------------------------------------------------------------- algebra minima

def cholesky(A):
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            v = A[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if v <= 0:
                    raise ValueError("matriz no definida positiva")
                L[i][j] = math.sqrt(v)
            else:
                L[i][j] = v / L[j][j]
    return L


def forward(L, b):           # resuelve L y = b
    y = [0.0] * len(b)
    for i in range(len(b)):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    return y


def backward(L, y):          # resuelve L^T x = y
    n = len(y)
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


# ---------------------------------------------------------------- modelo

def resumen(rows=(), base=None):
    """Lo que el ajuste necesita saber de unos ejemplos, en un tamano fijo: cuantos son, cuantas veces
    coincide cada par de tramos y las sumas de sus resultados. Ajustar con un resumen da lo mismo que
    ajustar con los ejemplos uno a uno, asi que los ejemplos antiguos se pueden guardar resumidos.
    Devuelve un resumen nuevo: el de `base` (si lo hay y vale) mas el de `rows`."""
    if base and len(base.get("b", ())) == DIM and len(base.get("A", ())) == DIM:
        r = {"n": base["n"], "sy": base["sy"], "yy": base["yy"], "b": list(base["b"]), "A": [list(f) for f in base["A"]]}
    else:
        r = {"n": 0, "sy": 0.0, "yy": 0.0, "b": [0.0] * DIM, "A": [[0] * DIM for _ in range(DIM)]}
    for idx, y in rows:
        r["n"] += 1
        r["sy"] += y
        r["yy"] += y * y
        for i in idx:
            r["b"][i] += y
            Ai = r["A"][i]
            for j in idx:
                Ai[j] += 1
    return r


def fit(rows, prior_bias, sigma0=45.0, tau=10.0, tau_bias=10.0, base=None):
    """rows: lista de (posiciones_activas, resultado_en_%). Devuelve el modelo ajustado.
    Creencia de partida: resultado medio = prior_bias (lo que cuestan las comisiones), y cada
    rasgo no cambia nada; tau dice cuanto se deja mover cada una antes de ver datos.
    `base`: resumen de ejemplos antiguos (ver `resumen`), que cuentan igual que los de `rows`."""
    r = resumen(rows, base)

    def solve(sig):
        s2 = sig * sig
        A = [[v / s2 for v in fila] for fila in r["A"]]
        b = [v / s2 for v in r["b"]]
        for i in range(DIM):
            A[i][i] += 1.0 / ((tau_bias if i == 0 else tau) ** 2)
        b[0] += prior_bias / (tau_bias ** 2)
        L = cholesky(A)
        return L, backward(L, forward(L, b))

    n = r["n"]
    sig = sigma0
    L, mean = solve(sig)
    if n >= 20:              # con suficientes ejemplos, el ruido se estima de los propios datos
        # suma de (resultado - estimacion)^2 de todos los ejemplos, sacada del resumen
        rss = r["yy"] - 2 * sum(m * v for m, v in zip(mean, r["b"])) \
            + sum(mean[i] * sum(a * m for a, m in zip(r["A"][i], mean)) for i in range(DIM))
        sig = max(15.0, math.sqrt(max(rss, 0.0) / (n - 1)))
        L, mean = solve(sig)
    return {"n": n, "sigma": sig, "mean": mean, "L": L}


def predice(m, idx):
    """Resultado esperado (en %) y su incertidumbre (desviacion tipica) para unos rasgos."""
    x = [0.0] * DIM
    for i in idx:
        x[i] = 1.0
    v = forward(m["L"], x)
    return sum(m["mean"][i] for i in idx), math.sqrt(sum(a * a for a in v))


def decide(models, feat, key):
    """Muestreo de Thompson. Devuelve (salida, valor_sorteado, valor_esperado) de la mejor salida.
    `key` fija el sorteo: la misma compra en el mismo instante decide siempre igual."""
    idx = vector(feat)
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))
    best = None
    for name, m in models.items():
        u = backward(m["L"], [rng.gauss(0.0, 1.0) for _ in range(DIM)])
        val = sum(m["mean"][i] + u[i] for i in idx)
        if best is None or val > best[1]:
            best = (name, val, sum(m["mean"][i] for i in idx))
    return best


def mejor(models, feat, z=0.0):
    """La mejor salida para unos rasgos: (salida, esperado, incertidumbre). Con z > 0 gana la que mejor
    queda tras restarle z veces su incertidumbre (su resultado "seguro"), no la de mayor esperado."""
    idx, best = vector(feat), None
    for name, m in models.items():
        mu, sd = predice(m, idx)
        if best is None or mu - z * sd > best[1] - z * best[2]:
            best = (name, mu, sd)
    return best


def efectos(m):
    """Cuanto mueve cada tramo el resultado esperado, con su incertidumbre: [(nombre, efecto, sd)]."""
    out, labels = [], nombres()
    for i in range(1, DIM):
        e = [0.0] * DIM
        e[i] = 1.0
        v = forward(m["L"], e)
        out.append((labels[i], m["mean"][i], math.sqrt(sum(a * a for a in v))))
    return out
