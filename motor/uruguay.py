"""Modelo del río Uruguay (Salto Grande → Concordia, Colón, Concepción del Uruguay).

Tres pasos, calibrados con datos reales desde 2011:
  1. Caudal que va a ENTRAR a Salto Grande en 1..10 días: caudales del río aguas arriba (Paso de los Libres,
     Santo Tomé, El Soberbio), represas de Brasil del Alto Uruguay (ONS), lluvia caída en 4 zonas de la cuenca y
     lluvia pronosticada (Open-Meteo).
  2. Caudal que va a SOLTAR la represa: lo anterior + nivel del embalse y cómo viene soltando (la represa guarda agua
     cuando el embalse está bajo y lo vacía antes de una crecida grande).
  3. ALTURA de cada puerto: curva altura–caudal soltado (datos 2006–2026) + una parte del desvío de hoy respecto de
     la curva, que se va diluyendo.

Todo dato pasa antes por limpiar(): se descartan vacíos, imposibles, picos de un día que salen y vuelven (con la escala
propia de cada serie) y tramos congelados (mismo valor repetido muchos días; no se aplica a represas).
Lluvia: Open-Meteo (CC BY 4.0) — histórica ERA5 (archive-api) y pronóstico (api.open-meteo.com).
"""
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from . import ina

SERIES = {"sg": 26674, "def": 26675, "cota": 26319, "libres": 26669, "stome": 26665, "sob": 26658,
          "conc": 26321, "colon": 80, "cdu": 81}
LIMITES = {"sg": (200, 45000), "def": (200, 45000), "libres": (200, 45000), "stome": (150, 45000), "sob": (50, 40000),
           "cota": (25, 38.5), "conc": (-0.5, 18), "colon": (-0.5, 13), "cdu": (-0.5, 11)}
CONGELADO = {"cota": 30, "conc": 20, "colon": 20, "cdu": 20}
REPRESAS_BR = ["FOZ_CHAPECO", "ITA", "PASSO_SAO_JOAO", "SAO_JOSE", "MONJOLINHO"]
ZONAS = {"alto": [(-27, -52), (-27.8, -51)], "medio": [(-28, -54), (-28.5, -55)],
         "ibicui": [(-29.5, -54.5), (-29.8, -55.8)], "corr": [(-28.5, -56.5), (-29.5, -57.5)]}
PESO_ZONA = {"alto": 0.35, "medio": 0.25, "ibicui": 0.25, "corr": 0.15}
HORIZONTES = [1, 2, 3, 5, 7, 10]
PUERTOS = {"conc": 79, "colon": 80, "cdu": 81}
DESDE_CAL = "2011-06-01"
LAM = 3.0

# Curvas altura–caudal soltado (mediana): Concordia con el promedio diario; Colón y C. del Uruguay con la lectura
# diaria, un día después de lo que suelta la represa.
CURVA = {
    "conc": [(500, .78), (1500, 1.70), (2500, 2.60), (3500, 3.40), (4500, 4.00), (5500, 4.80), (6500, 5.68), (7500, 6.38),
             (8500, 6.92), (9500, 7.60), (10500, 8.10), (11500, 8.66), (12500, 9.12), (13500, 9.60), (14500, 10.09),
             (15500, 10.50), (16500, 10.98), (17500, 11.38), (18500, 11.98), (19500, 12.22), (20500, 12.84),
             (21500, 12.88), (22500, 13.40), (23500, 13.58), (24500, 13.82), (25500, 14.50), (26500, 14.61),
             (27500, 15.30), (28500, 15.86)],
    "colon": [(1000, 1.2), (3000, 1.79), (5000, 2.57), (7000, 3.4), (9000, 4.12), (11000, 4.9), (13000, 5.7), (15000, 6.46),
              (17000, 7.0), (19000, 7.71), (21000, 8.37), (23000, 8.86), (25000, 9.26), (27000, 10.15)],
    "cdu": [(1000, 1.16), (3000, 1.54), (5000, 2.1), (7000, 2.74), (9000, 3.32), (11000, 3.88), (13000, 4.45), (15000, 5.08),
            (17000, 5.52), (19000, 6.13), (21000, 6.75), (23000, 7.1), (25000, 7.4), (27000, 8.16)]}
DEMORA = {"conc": 0, "colon": 1, "cdu": 1}


def curva(p, q):
    c = CURVA[p]
    if q <= c[0][0]:
        return c[0][1]
    for (a, ha), (b, hb) in zip(c, c[1:]):
        if q <= b:
            return ha + (hb - ha) * (q - a) / (b - a)
    (a, ha), (b, hb) = c[-2], c[-1]
    return hb + (hb - ha) * (q - b) / (b - a)


def sumar_dias(f, k):
    return (dt.date.fromisoformat(f) + dt.timedelta(days=k)).isoformat()


def _dias(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


# ------------------------------------------------------------------------------------------------ limpieza

def limpiar(serie, lo, hi, congelado=10):
    """Devuelve (serie_limpia, informe). Error = vacío/imposible, pico de un día que sale y vuelve (misma dirección
    respecto de ambos vecinos y vecinos parecidos entre sí) o tramo congelado. Nunca toca una subida o bajada pareja."""
    inf = {"imposibles": 0, "picos": 0, "congelados": 0}
    s = {}
    for f, v in serie.items():
        if v is None or v != v or v < lo or v > hi:
            inf["imposibles"] += 1
        else:
            s[f] = v
    k = sorted(s)
    ch = sorted(abs(s[b] - s[a]) for a, b in zip(k, k[1:]) if _dias(a, b) == 1)
    thr = max(150 if hi > 100 else 0.25, 1.5 * ch[int(0.99 * (len(ch) - 1))]) if len(ch) > 30 else float("inf")
    for _ in range(3):
        k = sorted(s)
        malos = []
        for a, x, c in zip(k, k[1:], k[2:]):
            va, vx, vc = s[a], s[x], s[c]
            dev = vx - (va + vc) / 2
            if abs(dev) > thr and abs(vc - va) < 0.3 * abs(dev) and (vx - va) * (vx - vc) > 0:
                malos.append(x)
        for x in malos:
            del s[x]
            inf["picos"] += 1
    k = sorted(s)
    i = 0
    while i < len(k):
        j = i
        while j + 1 < len(k) and s[k[j + 1]] == s[k[i]]:
            j += 1
        if j - i + 1 >= congelado:
            for t in range(i + 1, j + 1):
                del s[k[t]]
                inf["congelados"] += 1
        i = j + 1
    return s, inf


# ------------------------------------------------------------------------------------------------ datos

def series_ina(desde, hasta=None):
    hasta = hasta or (dt.date.today() + dt.timedelta(days=1)).isoformat()
    D, informe = {}, {}
    for k, sid in SERIES.items():
        crudo = ina.observaciones(sid, desde, hasta)
        D[k], informe[k] = limpiar(crudo, *LIMITES[k], CONGELADO.get(k, 10))
        time.sleep(0.5)
    return D, informe


def represas_br(desde):
    """{represa: {fecha: caudal soltado}} desde sige_hidro_represas (cargado por la tarea represas)."""
    from . import supa
    filas = supa.select("sige_hidro_represas", {"select": "represa,fecha,defluente_m3s",
                                                "represa": "in.(" + ",".join(REPRESAS_BR) + ")", "fecha": f"gte.{desde}"})
    B = {r: {} for r in REPRESAS_BR}
    for x in filas:
        if x["defluente_m3s"] is not None:
            B[x["represa"]][x["fecha"]] = float(x["defluente_m3s"])
    return {r: limpiar(v, 1, 40000, 10 ** 9)[0] for r, v in B.items()}


def _get_json(url, intentos=6):
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sige-motor"}), timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as ex:
            if ex.code == 429 and i < intentos - 1:
                time.sleep(70)
                continue
            raise


def lluvia_historica(desde, hasta):
    """Lluvia diaria ERA5 por zona (promedio de sus puntos): {zona: {fecha: mm}}. Respeta el límite de Open-Meteo."""
    Z = {z: {} for z in ZONAS}
    for z, pts in ZONAS.items():
        for la, lo in pts:
            d0 = dt.date.fromisoformat(desde)
            fin = dt.date.fromisoformat(hasta)
            while d0 <= fin:
                d1 = min(fin, d0 + dt.timedelta(days=5800))
                q = urllib.parse.urlencode({"latitude": la, "longitude": lo, "start_date": d0.isoformat(),
                                            "end_date": d1.isoformat(), "daily": "precipitation_sum",
                                            "timezone": "America/Sao_Paulo"})
                j = _get_json("https://archive-api.open-meteo.com/v1/archive?" + q)
                for f, v in zip(j["daily"]["time"], j["daily"]["precipitation_sum"]):
                    if v is not None and 0 <= v <= 250:
                        Z[z].setdefault(f, []).append(v)
                d0 = d1 + dt.timedelta(days=1)
                time.sleep(62)
    return {z: {f: sum(v) / len(v) for f, v in d.items()} for z, d in Z.items()}


def lluvia_reciente():
    """Lluvia de los últimos 60 días y pronóstico de 16 días (modelo meteorológico, no ERA5): {zona: {fecha: mm}}."""
    pts = [(z, la, lo) for z, P in ZONAS.items() for la, lo in P]
    q = urllib.parse.urlencode({"latitude": ",".join(str(p[1]) for p in pts), "longitude": ",".join(str(p[2]) for p in pts),
                                "daily": "precipitation_sum", "past_days": 60, "forecast_days": 16,
                                "timezone": "America/Sao_Paulo"}, safe=",")
    j = _get_json("https://api.open-meteo.com/v1/forecast?" + q)
    j = j if isinstance(j, list) else [j]
    Z = {z: {} for z in ZONAS}
    for (z, _, _), d in zip(pts, j):
        for f, v in zip(d["daily"]["time"], d["daily"]["precipitation_sum"]):
            if v is not None and 0 <= v <= 250:
                Z[z].setdefault(f, []).append(v)
    return {z: {f: sum(v) / len(v) for f, v in d.items()} for z, d in Z.items()}


# ------------------------------------------------------------------------------------------------ rasgos

def _suma(R, z, f, a, b):
    s = 0.0
    for k in range(a, b + 1):
        v = R[z].get(sumar_dias(f, k))
        if v is None:
            return None
        s += v
    return s


def rasgos(D, R, B, f):
    """Rasgos para pronosticar desde el día f (paso 1)."""
    g = lambda k, d=0: D[k].get(sumar_dias(f, -d))
    req = [g("sg"), g("libres"), g("stome"), g("sob"), g("libres", 2), g("stome", 3), g("sob", 3), g("sg", 3)]
    if any(v is None or v <= 0 for v in req):
        return None
    sg, li, st, so, li2, st3, so3, sg3 = [v / 1000 for v in req]
    x = [li - sg, st - sg, so - sg, li - li2, st - st3, so - so3, sg - sg3, sg]
    for z in ZONAS:
        for a, b in ((-3, 0), (-7, -4), (-14, -8), (1, 3), (4, 7)):
            v = _suma(R, z, f, a, b)
            if v is None:
                return None
            x.append(v)
    for r in REPRESAS_BR:
        a, b = B.get(r, {}).get(f), B.get(r, {}).get(sumar_dias(f, -2))
        if not a or not b:
            return None
        x += [a / 1000, (a - b) / 1000]
    return x


def rasgos2(D, R, B, f):
    """Rasgos para lo que suelta la represa (paso 2)."""
    x = rasgos(D, R, B, f)
    c = D["cota"].get(f, D["cota"].get(sumar_dias(f, -1)))
    de, de3 = D["def"].get(f), D["def"].get(sumar_dias(f, -3))
    if x is None or c is None or de is None or de3 is None:
        return None
    return x + [c - 34.5, max(0, c - 35), max(0, 33.5 - c), (de - D["sg"][f]) / 1000, de / 1000, (de - de3) / 1000]


def ajustar(X, y, lam=LAM):
    X, y = np.asarray(X, float), np.asarray(y, float)
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    my = y.mean()
    w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - my))
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "my": float(my)}


def aplicar(m, x):
    return m["my"] + float(np.dot((np.asarray(x) - m["mu"]) / np.asarray(m["sd"]), m["w"]))


def calibrar_modelos(D, R, B, hasta):
    """Modelos por horizonte (cambio de caudal en miles de m³/s) + factor de desvío por puerto."""
    dias = sorted(f for f in D["sg"] if DESDE_CAL <= f < hasta)
    M = {"entra": {}, "sale": {}}
    X1c = {f: rasgos(D, R, B, f) for f in dias}
    X2c = {f: rasgos2(D, R, B, f) for f in dias}
    for k in HORIZONTES:
        X1, y1, X2, y2 = [], [], [], []
        for f in dias:
            t = sumar_dias(f, k)
            if t >= hasta:
                continue
            x = X1c[f]
            if x is not None and D["sg"].get(t):
                X1.append(x)
                y1.append((D["sg"][t] - D["sg"][f]) / 1000)
            x2 = X2c[f]
            if x2 is not None and D["def"].get(t):
                X2.append(x2)
                y2.append((D["def"][t] - D["def"][f]) / 1000)
        M["entra"][k] = ajustar(X1, y1)
        M["sale"][k] = ajustar(X2, y2)
    # Factor alfa: cuánto del desvío de hoy respecto de la curva se mantiene (0 = el puerto vuelve a la curva)
    M["alfa"] = {}
    for p in PUERTOS:
        mejor = (1e9, 0.5)
        for a in (0, .25, .5, .75, 1):
            e, n = 0.0, 0
            for f in dias[::3]:
                h0, o0 = D[p].get(f), D["def"].get(f)
                t = sumar_dias(f, 5)
                h1, o1 = D[p].get(t), D["def"].get(sumar_dias(t, -DEMORA[p]))
                if None in (h0, o0, h1, o1) or t >= hasta:
                    continue
                e += (curva(p, o1) + a * (h0 - curva(p, o0)) - h1) ** 2
                n += 1
            if n and e / n < mejor[0]:
                mejor = (e / n, a)
        M["alfa"][p] = mejor[1]
    return M


def normalizar(M):
    """Las claves de horizonte vuelven como texto al leer el modelo desde la base."""
    for c in ("entra", "sale"):
        M[c] = {int(k): v for k, v in M[c].items()}
    return M


def pronosticar(M, D, R, B, f):
    """Pronóstico desde el día f: {k: {fecha, q_entra, q_sale, alturas}} o None si faltan datos."""
    x, x2 = rasgos(D, R, B, f), rasgos2(D, R, B, f)
    if x is None or x2 is None:
        return None
    sg0, de0 = D["sg"][f], D["def"][f]
    Q = {0: (sg0, de0)}
    for k in HORIZONTES:
        Q[k] = (max(200.0, sg0 + aplicar(M["entra"][k], x) * 1000), max(200.0, de0 + aplicar(M["sale"][k], x2) * 1000))

    def sale_en(d):   # caudal soltado interpolado para cualquier día 0..10
        ks = sorted(Q)
        d = max(0, min(ks[-1], d))
        for a, b in zip(ks, ks[1:]):
            if a <= d <= b:
                return Q[a][1] + (Q[b][1] - Q[a][1]) * (d - a) / (b - a)
        return Q[ks[-1]][1]

    out = {}
    for k in HORIZONTES:
        alt = {}
        for p in PUERTOS:
            h0 = D[p].get(f) or D[p].get(sumar_dias(f, -1))
            if h0 is None:
                alt[p] = None
                continue
            a = M["alfa"][p]
            alt[p] = round(curva(p, sale_en(k - DEMORA[p])) + a * (h0 - curva(p, de0)), 2)
        out[k] = {"fecha": sumar_dias(f, k), "q_entra": round(Q[k][0]), "q_sale": round(Q[k][1]), **alt}
    return out


# ------------------------------------------------------------------------------------------------ prueba

def evaluar(D, R, B, corte="2021-01-01", log=print):
    """Calibra con datos hasta `corte` y pronostica cada día posterior. Reporta el error en todos los días y en los días
    posteriores a una lluvia intensa (≥ 45 mm de promedio en la cuenca en 3 días). Usa la lluvia que realmente cayó como
    si fuera el pronóstico: en la operación real el error va a ser algo mayor."""
    M = calibrar_modelos(D, R, B, corte)
    Rc = {}
    for f in R["alto"]:
        v = [R[z].get(f) for z in ZONAS]
        if None not in v:
            Rc[f] = sum(PESO_ZONA[z] * R[z][f] for z in ZONAS)
    lluvia3 = lambda f: sum(Rc.get(sumar_dias(f, -d), 0) for d in range(3))
    eventos = [f for f in sorted(Rc) if f >= corte and lluvia3(f) >= 45]
    ev = set(eventos)
    PP = {f: pronosticar(M, D, R, B, f) for f in sorted(D["sg"]) if f >= corte}
    res = {}
    for k in HORIZONTES:
        e = {p: [] for p in ["entra", "sale", *PUERTOS]}
        ee = {p: [] for p in e}
        pe = {p: [] for p in e}
        for f, P in PP.items():
            if not P:
                continue
            t = P[k]["fecha"]
            real = {"entra": D["sg"].get(t), "sale": D["def"].get(t), **{p: D[p].get(t) for p in PUERTOS}}
            hoy = {"entra": D["sg"][f], "sale": D["def"][f], **{p: D[p].get(f) for p in PUERTOS}}
            evento = any(sumar_dias(f, -d) in ev for d in (0, 1, 2))
            for p in e:
                pr = P[k]["q_" + p] if p in ("entra", "sale") else P[k][p]
                if real[p] is None or pr is None or hoy[p] is None:
                    continue
                e[p].append(pr - real[p])
                if evento:
                    ee[p].append(pr - real[p])
                    pe[p].append(hoy[p] - real[p])
        rm = lambda v: (sum(z * z for z in v) / len(v)) ** 0.5 if v else float("nan")
        res[k] = {p: {"todo": rm(e[p]), "lluvia": rm(ee[p]), "sin_modelo": rm(pe[p]), "n": len(ee[p])} for p in e}
        log(f"uruguay a {k:2d} días: " + " · ".join(
            f"{p} {res[k][p]['lluvia']:.0f} (sin modelo {res[k][p]['sin_modelo']:.0f})" if p in ("entra", "sale") else
            f"{p} {res[k][p]['lluvia']:.2f} m (sin modelo {res[k][p]['sin_modelo']:.2f})" for p in e)
            + f" · días tras lluvia intensa: {res[k]['entra']['n']}")
    # detalle de los eventos de lluvia intensa: pronóstico a 7 días vs lo que marcó Concordia
    vistos = set()
    for f in eventos:
        P = PP.get(f)
        if not P or any(_dias(v, f) < 10 for v in vistos):
            continue
        vistos.add(f)
        t = P[7]["fecha"]
        hc, h0 = D["conc"].get(t), D["conc"].get(f)
        if hc is None or h0 is None or P[7]["conc"] is None:
            continue
        log(f"  evento {f} · llovió {lluvia3(f):.0f} mm · Concordia {h0:.2f} → real {hc:.2f} / pronóstico {P[7]['conc']:.2f}"
            f" (error {P[7]['conc'] - hc:+.2f} m) · Salto Grande soltó {D['def'].get(t, 0):.0f} / pronóstico {P[7]['q_sale']}")
    return M, res
