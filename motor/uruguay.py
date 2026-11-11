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

from . import ina, om

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
            with urllib.request.urlopen(urllib.request.Request(om.firmar(url), headers={"User-Agent": "sige-motor"}), timeout=120) as r:
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


VARIANTES = {   # combinaciones de mejoras que compara evaluar()
    "base": {},
    "base_suave": {"alfa": 1, "gamma": 1, "lam": 30},
    "+escalas_brasil": {"alfa": 1, "gamma": 1, "lam": 30, "ana": 1},
    "+escalas_brasil_suave": {"alfa": 1, "gamma": 1, "lam": 80, "ana": 1},
}
ANA = ["a_irai", "a_ijui", "a_ibicui", "a_alegrete", "a_sborja"]   # escalas de Brasil (ver ana.py)
MEJORAS = {"alfa": 1, "gamma": 1, "lam": 80, "ana": 1}   # pronóstico diario: ganó "+escalas_brasil_suave" (evaluar_uruguay, nov 2026)
MEJORAS_SIN_ANA = {"alfa": 1, "gamma": 1, "lam": 30}   # respaldo si las escalas de Brasil no responden


def _fl(mej):
    if mej is True:
        return dict(MEJORAS)
    return dict(mej or {})


def _rc(R, f, a, b):
    """Lluvia de la cuenca (promedio ponderado de las zonas) entre los días f+a y f+b."""
    s = 0.0
    for z in ZONAS:
        v = _suma(R, z, f, a, b)
        if v is None:
            return None
        s += PESO_ZONA[z] * v
    return s


def rasgos(D, R, B, f, mej=None):
    """Rasgos para pronosticar desde el día f (paso 1). mej=True agrega las mejoras de noviembre 2026: tendencia del
    último día, suelo mojado (lluvia × cuán alto viene el río y × lluvia de las semanas anteriores) y lluvia fuerte."""
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
    if _fl(mej).get("ana"):
        for s in ANA:
            serie = D.get(s, {})
            v0 = serie.get(f) or serie.get(sumar_dias(f, -1))
            v2 = serie.get(sumar_dias(f, -2)) or serie.get(sumar_dias(f, -3))
            if not v0 or not v2:
                return None
            x += [v0 / 1000, (v0 - v2) / 1000]
        x.append((D["a_irai"].get(f) or D["a_irai"][sumar_dias(f, -1)]) / 1000 - so)
    if _fl(mej).get("rasgos"):
        sg1, li1 = g("sg", 1), g("libres", 1)
        ante = _rc(R, f, -30, -8)
        w = [_rc(R, f, a, b) for a, b in ((-3, 0), (-7, -4), (1, 3), (4, 7))]
        if sg1 is None or li1 is None or ante is None or None in w:
            return None
        x += [sg - sg1 / 1000, li - li1 / 1000]
        for v in w:
            x += [v * sg / 10, v * ante / 100]
        x += [w[0] ** 2 / 50, (w[2] + w[3]) ** 2 / 50]
    return x


def rasgos2(D, R, B, f, mej=None):
    """Rasgos para lo que suelta la represa (paso 2). Con mejoras: cuánto suele guardar cuando el embalse está bajo."""
    x = rasgos(D, R, B, f, mej)
    c = D["cota"].get(f, D["cota"].get(sumar_dias(f, -1)))
    de, de3 = D["def"].get(f), D["def"].get(sumar_dias(f, -3))
    if x is None or c is None or de is None or de3 is None:
        return None
    x = x + [c - 34.5, max(0, c - 35), max(0, 33.5 - c), (de - D["sg"][f]) / 1000, de / 1000, (de - de3) / 1000]
    if _fl(mej).get("rasgos"):
        de1, sg3 = D["def"].get(sumar_dias(f, -1)), D["sg"].get(sumar_dias(f, -3))
        llu = _rc(R, f, -7, 7)
        if de1 is None or sg3 is None or llu is None:
            return None
        bajo = max(0, 34.5 - c)
        x += [(de - de1) / 1000, bajo * llu / 50, bajo * (D["sg"][f] - sg3) / 1000]
    return x


def ajustar(X, y, lam=LAM, peso=None):
    X, y = np.asarray(X, float), np.asarray(y, float)
    p = np.ones(len(y)) if peso is None else np.asarray(peso, float)
    mu = (X * p[:, None]).sum(0) / p.sum()
    sd = np.sqrt(((X - mu) ** 2 * p[:, None]).sum(0) / p.sum())
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    my = float((y * p).sum() / p.sum())
    Zp = Z * p[:, None]
    w = np.linalg.solve(Z.T @ Zp + lam * np.eye(Z.shape[1]), Zp.T @ (y - my))
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "my": my}


def aplicar(m, x):
    return m["my"] + float(np.dot((np.asarray(x) - m["mu"]) / np.asarray(m["sd"]), m["w"]))


def _peso_crecida(D, R, f):
    """Los días de crecida o de lluvia fuerte pesan 3 veces más al entrenar (son los que importan para alertas)."""
    l3 = _rc(R, f, -3, 0) or 0
    return 3.0 if l3 >= 30 or D["sg"].get(f, 0) >= 10000 else 1.0


def calibrar_modelos(D, R, B, hasta, mej=True):
    mej = _fl(mej)
    lam = mej.get("lam", LAM)
    """Modelos por horizonte (cambio de caudal en miles de m³/s) + factor de desvío por puerto (y por horizonte con
    mejoras) + corrección con el error de ayer (con mejoras)."""
    dias = sorted(f for f in D["sg"] if DESDE_CAL <= f < hasta)
    M = {"entra": {}, "sale": {}, "mej": mej}
    X1c = {f: rasgos(D, R, B, f, mej) for f in dias}
    X2c = {f: rasgos2(D, R, B, f, mej) for f in dias}
    pc = {f: (_peso_crecida(D, R, f) if mej.get("peso") else 1.0) for f in dias}
    for k in HORIZONTES:
        X1, y1, p1, X2, y2, p2 = [], [], [], [], [], []
        for f in dias:
            t = sumar_dias(f, k)
            if t >= hasta:
                continue
            x = X1c[f]
            if x is not None and D["sg"].get(t):
                X1.append(x)
                y1.append((D["sg"][t] - D["sg"][f]) / 1000)
                p1.append(pc[f])
            x2 = X2c[f]
            if x2 is not None and D["def"].get(t):
                X2.append(x2)
                y2.append((D["def"][t] - D["def"][f]) / 1000)
                p2.append(pc[f])
        M["entra"][k] = ajustar(X1, y1, lam, peso=p1)
        M["sale"][k] = ajustar(X2, y2, lam, peso=p2)
    # Factor alfa: cuánto del desvío de hoy respecto de la curva se mantiene (0 = el puerto vuelve a la curva).
    # Con mejoras se calcula para cada horizonte: al día siguiente el puerto todavía se parece mucho a hoy.
    M["alfa"] = {}
    grilla = [i / 10 for i in range(11)] if mej.get("alfa") else [0, .25, .5, .75, 1]
    for p in PUERTOS:
        M["alfa"][p] = {}
        for k in (HORIZONTES if mej.get("alfa") else [5]):
            mejor = (1e9, 0.5)
            for a in grilla:
                e, n = 0.0, 0
                for f in dias[::2]:
                    h0, o0 = D[p].get(f), D["def"].get(f)
                    t = sumar_dias(f, k)
                    h1, o1 = D[p].get(t), D["def"].get(sumar_dias(t, -DEMORA[p]))
                    if None in (h0, o0, h1, o1) or t >= hasta:
                        continue
                    e += (curva(p, o1) + a * (h0 - curva(p, o0)) - h1) ** 2
                    n += 1
                if n and e / n < mejor[0]:
                    mejor = (e / n, a)
            M["alfa"][p][k] = mejor[1]
        if not mej.get("alfa"):
            M["alfa"][p] = {k: M["alfa"][p][5] for k in HORIZONTES}
    # Corrección con el error de ayer: si ayer el pronóstico a 1 día se pasó (o se quedó corto), los de hoy tienden a
    # equivocarse para el mismo lado. gamma[var][k] = cuánto de ese error se descuenta.
    M["gamma"] = {}
    if mej.get("gamma"):
        crudo = {f: _pronostico_crudo(M, D, R, B, f) for f in dias}
        reales = lambda t: {"q_entra": D["sg"].get(t), "q_sale": D["def"].get(t), **{p: D[p].get(t) for p in PUERTOS}}
        for var in ["q_entra", "q_sale", *PUERTOS]:
            M["gamma"][var] = {}
            for k in HORIZONTES:
                num = den = 0.0
                for f in dias:
                    ay = crudo.get(sumar_dias(f, -1))
                    P = crudo.get(f)
                    t = sumar_dias(f, k)
                    if not ay or not P or t >= hasta:
                        continue
                    r0, rk = reales(f)[var], reales(t)[var]
                    if r0 is None or rk is None or ay[1][var] is None or P[k][var] is None:
                        continue
                    e1, ek = ay[1][var] - r0, P[k][var] - rk
                    num += e1 * ek
                    den += e1 * e1
                M["gamma"][var][k] = round(float(min(1.0, max(0.0, num / den))), 3) if den else 0.0
    return M


def normalizar(M):
    """Las claves de horizonte vuelven como texto al leer el modelo desde la base."""
    for c in ("entra", "sale"):
        M[c] = {int(k): v for k, v in M[c].items()}
    M["alfa"] = {p: ({int(k): v for k, v in a.items()} if isinstance(a, dict) else {k: a for k in HORIZONTES})
                 for p, a in M["alfa"].items()}
    M["gamma"] = {v: {int(k): g for k, g in d.items()} for v, d in M.get("gamma", {}).items()}
    M.setdefault("mej", {})
    if M["mej"] is True:   # modelo guardado antes de las variantes
        M["mej"] = {"alfa": 1, "gamma": 1, "rasgos": 1, "peso": 1}
    return M


def _pronostico_crudo(M, D, R, B, f):
    mej = M.get("mej", {})
    x, x2 = rasgos(D, R, B, f, mej), rasgos2(D, R, B, f, mej)
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
            a = M["alfa"][p][k]
            alt[p] = curva(p, sale_en(k - DEMORA[p])) + a * (h0 - curva(p, de0))
        out[k] = {"fecha": sumar_dias(f, k), "q_entra": Q[k][0], "q_sale": Q[k][1], **alt}
    return out


def pronosticar(M, D, R, B, f):
    """Pronóstico desde el día f: {k: {fecha, q_entra, q_sale, alturas}} o None si faltan datos."""
    P = _pronostico_crudo(M, D, R, B, f)
    if P is None:
        return None
    if M.get("gamma"):
        ay = _pronostico_crudo(M, D, R, B, sumar_dias(f, -1))
        real = {"q_entra": D["sg"].get(f), "q_sale": D["def"].get(f), **{p: D[p].get(f) for p in PUERTOS}}
        if ay:
            for var, g in M["gamma"].items():
                if real[var] is None or ay[1][var] is None:
                    continue
                e1 = ay[1][var] - real[var]
                for k in HORIZONTES:
                    if P[k][var] is not None:
                        P[k][var] -= g[k] * e1
    for k in HORIZONTES:
        P[k]["q_entra"] = round(max(200.0, P[k]["q_entra"]))
        P[k]["q_sale"] = round(max(200.0, P[k]["q_sale"]))
        for p in PUERTOS:
            if P[k][p] is not None:
                P[k][p] = round(float(P[k][p]), 2)
    return P


# ------------------------------------------------------------------------------------------------ prueba

def _medir(PP, D, ev, corte):
    rm = lambda v: (sum(z * z for z in v) / len(v)) ** 0.5 if v else float("nan")
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
        res[k] = {p: {"todo": rm(e[p]), "lluvia": rm(ee[p]), "sin_modelo": rm(pe[p]), "n": len(ee[p])} for p in e}
    return res


def evaluar(D, R, B, corte="2021-01-01", log=print):
    """Calibra con datos hasta `corte` y pronostica cada día posterior con cada combinación de mejoras (VARIANTES), sobre
    los mismos días. Reporta el error en los días posteriores a una lluvia intensa (≥ 45 mm en 3 días en la cuenca) y en
    todos los días. Usa la lluvia que realmente cayó como si fuera el pronóstico: en la operación real será algo mayor."""
    Rc = {}
    for f in R["alto"]:
        v = [R[z].get(f) for z in ZONAS]
        if None not in v:
            Rc[f] = sum(PESO_ZONA[z] * R[z][f] for z in ZONAS)
    lluvia3 = lambda f: sum(Rc.get(sumar_dias(f, -d), 0) for d in range(3))
    eventos = [f for f in sorted(Rc) if f >= corte and lluvia3(f) >= 45]
    ev = set(eventos)
    dias = [f for f in sorted(D["sg"]) if f >= corte]
    MM, PP = {}, {}
    for nombre, mej in VARIANTES.items():
        MM[nombre] = calibrar_modelos(D, R, B, corte, mej=mej)
        PP[nombre] = {f: pronosticar(MM[nombre], D, R, B, f) for f in dias}
    comunes = [f for f in dias if all(PP[n][f] for n in PP)]   # mismos días para comparar en forma justa
    RR = {n: _medir({f: PP[n][f] for f in comunes}, D, ev, corte) for n in PP}
    for clave, titulo in (("lluvia", "TRAS LLUVIA INTENSA"), ("todo", "TODOS LOS DÍAS")):
        log(f"uruguay {titulo} (n {RR['base'][1]['entra']['n'] if clave == 'lluvia' else len(comunes)}) — error por variante:")
        for k in HORIZONTES:
            log(f"  a {k:2d} días · sin modelo: Concordia {RR['base'][k]['conc']['sin_modelo']:.2f}")
            for n in RR:
                r = RR[n][k]
                log(f"     {n:22s} Concordia {r['conc'][clave]:.2f} · Colón {r['colon'][clave]:.2f} · CdU {r['cdu'][clave]:.2f}"
                    f" · entra {r['entra'][clave]:.0f} · sale {r['sale'][clave]:.0f}")
    # puntaje: error medio de los 3 puertos en días tras lluvia intensa, todos los horizontes
    pun = {n: sum(RR[n][k][p]["lluvia"] for k in HORIZONTES for p in PUERTOS) / (3 * len(HORIZONTES)) for n in RR}
    pun = {n: (v if v == v else float("inf")) for n, v in pun.items()}
    mejor = min(pun, key=pun.get)
    log("uruguay puntaje (error medio de los puertos tras lluvia intensa): " +
        " · ".join(f"{n} {v:.3f}" for n, v in sorted(pun.items(), key=lambda x: x[1])) + f" → mejor: {mejor}")
    P0, P1 = PP["base"], PP[mejor]
    vistos = set()
    for f in eventos:
        if f not in comunes or any(_dias(v, f) < 10 for v in vistos):
            continue
        vistos.add(f)
        t = P1[f][7]["fecha"]
        hc, h0 = D["conc"].get(t), D["conc"].get(f)
        if hc is None or h0 is None or P1[f][7]["conc"] is None:
            continue
        log(f"  evento {f} · {lluvia3(f):.0f} mm · Concordia {h0:.2f} → real {hc:.2f} · base {P0[f][7]['conc']:.2f}"
            f" · {mejor} {P1[f][7]['conc']:.2f} · SG soltó {D['def'].get(t, 0):.0f} / {P1[f][7]['q_sale']}")
    return MM[mejor], RR[mejor]
