"""Calibración de la proyección (método directo por horizonte, adaptado de Vigía del Paraná).

Para cada puerto y horizonte k = 1..15 días se ajustan pesos w (regresión ridge) sobre los cambios de la
última semana y la semana anterior en el propio puerto y en hasta 8 puertos de aguas arriba. Las entradas
se eligen de a una (selección hacia adelante) con validación cruzada por bloques de tiempo; el error fuera
de muestra (RMSE) es la banda de error. Un puerto solo recibe modelo si mejora a suponer que el río no cambia.

Required Notice: Copyright (c) 2026 Melisa Franzotti — Vigía del Paraná (https://vigia-del-parana.github.io)
"""
import datetime as dt
import math
import numpy as np

H = 15
VENTANA = 8
MAX_ENTRADAS = 4
MEJORA_MIN = 0.005
LAMBDA = 1.0
BLOQUES = 4
SUBE_7D = 0.15
VENTANAS = [(0, 3), (0, 7), (7, 14)]   # últimos 3 días, última semana y semana anterior
# Represas como entradas: id ficticio -> qué puertos pueden usarla (descarga en miles de m³/s)
ITAIPU_ID = 1000


def cuenca(e):
    """Paraná y Paraguay forman una sola red (el Paraguay entra al Paraná); el Uruguay es otra."""
    return "URUGUAY" if e["rio"].startswith("Uruguay") else "PARANA"


def diaria(serie: dict, d0: dt.date, d1: dt.date):
    """Arreglo diario continuo; huecos de hasta 5 días se interpolan, más largos quedan NaN."""
    n = (d1 - d0).days + 1
    v = [serie.get((d0 + dt.timedelta(days=i)).isoformat()) for i in range(n)]
    i = 0
    while i < n:
        if v[i] is None:
            j = i
            while j < n and v[j] is None:
                j += 1
            if 0 < i and j < n and j - i <= 5:
                a, b = v[i - 1], v[j]
                for k in range(i, j):
                    v[k] = a + (b - a) * (k - i + 1) / (j - i + 1)
            i = j
        else:
            i += 1
    return np.array([np.nan if x is None else x for x in v], dtype=float)


def _tabla(S, did, feats):
    hd = S[did]; n = len(hd); T = np.arange(14, n)
    X = np.stack([S[s][T - a] - S[s][T - b] for s, a, b in feats], 1) if feats else np.zeros((len(T), 0))
    Y = np.full((len(T), H), np.nan)
    for k in range(1, H + 1):
        m = T + k < n
        Y[m, k - 1] = hd[T[m] + k] - hd[T[m]]
    ok = ~np.isnan(X).any(1) & ~np.isnan(hd[T])
    sube = (hd[T] - hd[T - 7]) > SUBE_7D
    return T[ok], X[ok], Y[ok], sube[ok]


def _ajustar(X, y):
    m = ~np.isnan(y)
    if m.sum() < 100:
        return None
    A = X[m]
    return np.linalg.solve(A.T @ A + LAMBDA * np.eye(A.shape[1]), A.T @ y[m])


def _validar(T, X, Y, sube):
    if len(T) < 200:
        return None
    bordes = np.linspace(T[0], T[-1] + 1, BLOQUES + 1).astype(int)
    E = np.full(Y.shape, np.nan)
    for f in range(BLOQUES):
        lo, hi = bordes[f], bordes[f + 1]
        te = (T >= lo) & (T < hi); tr = (T < lo - H) | (T >= hi + H)
        for k in range(H):
            w = _ajustar(X[tr], Y[tr, k])
            if w is None:
                return None
            E[te, k] = X[te] @ w - Y[te, k]

    def rmse(M, minimo):
        out = []
        for k in range(H):
            e = M[:, k]; e = e[~np.isnan(e)]
            out.append(round(float(np.sqrt(np.mean(e * e))), 3) if len(e) >= minimo else None)
        return out
    return rmse(E, 50), rmse(E[sube], 20)


def _puntaje(err):
    v = [x for x in err[:10] if x is not None]
    return sum(v) / len(v) if len(v) >= 8 else None


def _persistencia(hd):
    out = []
    for k in range(1, H + 1):
        e = hd[k:] - hd[:-k]; e = e[~np.isnan(e)]
        out.append(round(float(np.sqrt(np.mean(e * e))), 3) if len(e) else None)
    return out


def calibrar(estaciones: list, series: dict, hasta: dt.date, anios: int = 3, log=print, extras: dict | None = None):
    """`series[id]` = {fecha: altura}. `extras` = {id: nombre} de entradas no hidrométricas (p. ej. descarga de
    Itaipú en miles de m³/s, id 1000) que pueden usar los puertos del río Paraná.
    Devuelve {id: {entradas, w, err, err_sube}} solo para puertos con modelo útil."""
    est = [e for e in estaciones if not e.get("marea")]
    extras = {k: v for k, v in (extras or {}).items() if k in series}
    d0 = hasta - dt.timedelta(days=365 * anios)
    S = {e["id"]: diaria(series.get(e["id"], {}), d0, hasta) for e in est}
    S.update({k: diaria(series[k], d0, hasta) for k in extras})
    nombre = {e["id"]: e["nombre"] for e in est}
    nombre.update(extras)
    modelo = {}
    for e in sorted(est, key=lambda x: x["orden"]):
        did = e["id"]
        if np.isnan(S[did]).mean() > 0.4:
            log(f"{e['nombre']}: pocos datos, sin modelo"); continue
        cands = [u["id"] for u in est if cuenca(u) == cuenca(e) and u["orden"] <= e["orden"] and e["orden"] - u["orden"] <= VENTANA and np.isnan(S[u["id"]]).mean() < 0.4]
        if e["rio"].startswith("Paraná"):
            cands += [k for k in extras if np.isnan(S[k]).mean() < 0.4]
        pool = [(s, a, b) for s in cands for a, b in VENTANAS]
        elegidas, mejor, val = [], None, None
        while len(elegidas) < MAX_ENTRADAS:
            prueba = []
            for f in pool:
                if f in elegidas:
                    continue
                v = _validar(*_tabla(S, did, elegidas + [f]))
                sc = _puntaje(v[0]) if v else None
                if sc is not None:
                    prueba.append((sc, f, v))
            if not prueba:
                break
            prueba.sort(key=lambda z: z[0])
            sc, f, v = prueba[0]
            if mejor is not None and sc > mejor * (1 - MEJORA_MIN):
                break
            elegidas.append(f); mejor, val = sc, v
        pers = _persistencia(S[did])
        if val and None not in val[0][:7] and sum(val[0][:7]) < sum(x for x in pers[:7] if x is not None):
            T, X, Y, _ = _tabla(S, did, elegidas)
            W = [[round(float(w), 4) for w in _ajustar(X, Y[:, k])] for k in range(H)]
            modelo[str(did)] = {"entradas": [{"id": s, "nombre": nombre[s], "desde": a, "hasta": b} for s, a, b in elegidas],
                                "w": W, "err": val[0], "err_sube": val[1]}
            log(f"{e['nombre']}: modelo con {len(elegidas)} entradas, error a 7 días {val[0][6]} m")
        else:
            log(f"{e['nombre']}: sin modelo mejor que persistencia")
    return modelo
