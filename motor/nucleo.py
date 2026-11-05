"""Núcleo de cálculo del motor hidrológico SIGE (sin red ni base de datos: funciones puras).

Proyección por puerto (método directo por horizonte, adaptado de Vigía del Paraná):
    h(t+k) = h(t) + Σ_i w[k][i] · ( h_si(t − desde_i) − h_si(t − hasta_i) ),   k = 1..15 días
Cruces de nivel: interpolación lineal entre valores diarios (dato de las 00 h, hora argentina) para
estimar el momento en que la altura cruza la alerta o la evacuación; la banda de error da el rango.

Required Notice: Copyright (c) 2026 Melisa Franzotti — Vigía del Paraná (https://vigia-del-parana.github.io)
Uso bajo PolyForm Noncommercial 1.0.0 (https://polyformproject.org/licenses/noncommercial/1.0.0).
"""
from __future__ import annotations
import datetime as dt

ART = dt.timezone(dt.timedelta(hours=-3))
HORA = 3600.0


def t00(fecha: str) -> dt.datetime:
    """00 h hora argentina del día `fecha` (YYYY-MM-DD)."""
    return dt.datetime.fromisoformat(fecha).replace(tzinfo=ART)


def sumar_dias(fecha: str, k: int) -> str:
    return (dt.date.fromisoformat(fecha) + dt.timedelta(days=k)).isoformat()


def cambio_entrada(serie: dict, t: str, desde: int, hasta: int, tolerancia: int = 4):
    """h(t−desde) − h(t−hasta); si falta el dato del día, corre las dos puntas hasta `tolerancia` días."""
    for off in range(tolerancia + 1):
        a, b = serie.get(sumar_dias(t, -desde - off)), serie.get(sumar_dias(t, -hasta - off))
        if a is not None and b is not None:
            return a - b
    return None


def proyectar(series: dict[int, dict], modelo: dict, est_id: int, sube_7d: float = 0.15):
    """Devuelve [{k, fecha, central, min, max, sube}] o None. `series[id]` = {fecha: altura}."""
    M = modelo.get(str(est_id))
    s = series.get(est_id)
    if not M or not M.get("w") or not s:
        return None
    t = max(s)
    h0 = s[t]
    x = []
    for en in M["entradas"]:
        u = series.get(en["id"])
        v = cambio_entrada(u, t, en["desde"], en["hasta"]) if u else None
        if v is None:
            return None
        x.append(v)
    h7 = s.get(sumar_dias(t, -7))
    sube = h7 is not None and (h0 - h7) > sube_7d and bool(M.get("err_sube"))
    errs = M["err_sube"] if sube else M["err"]
    out = []
    for k, w in enumerate(M["w"], start=1):
        c = h0 + sum(wi * xi for wi, xi in zip(w, x))
        e = errs[k - 1] if errs and errs[k - 1] is not None else (M["err"][k - 1] if M.get("err") else None)
        out.append({"k": k, "fecha": sumar_dias(t, k), "central": round(c, 2),
                    "min": round(c - e, 2) if e is not None else None,
                    "max": round(c + e, 2) if e is not None else None, "sube": sube})
    return out


def _curva(fecha0: str, h0: float, proy: list, campo: str):
    return [(t00(fecha0), h0)] + [(t00(q["fecha"]), q[campo] if q[campo] is not None else q["central"]) for q in proy]


def _cruce(curva, nivel: float, sentido: int, despues: dt.datetime | None = None):
    for (ta, ha), (tb, hb) in zip(curva, curva[1:]):
        if despues is not None and tb <= despues:
            continue
        if (ha < nivel <= hb) if sentido > 0 else (ha >= nivel > hb):
            f = (nivel - ha) / (hb - ha)
            return (ta + (tb - ta) * f).replace(second=0, microsecond=0)
    return None


def _desde_historia(serie: dict, nivel: float):
    fechas = sorted(serie)
    i = len(fechas) - 1
    if serie[fechas[i]] < nivel:
        return None, False
    while i > 0 and serie[fechas[i - 1]] >= nivel:
        i -= 1
    if i == 0:
        return t00(fechas[0]), True
    a, b = fechas[i - 1], fechas[i]
    f = (nivel - serie[a]) / (serie[b] - serie[a])
    return t00(a) + (t00(b) - t00(a)) * f, False


def cruces(serie: dict, proy: list, alerta: float, evac: float):
    """Eventos de cruce por nivel: ENTRA / SALE / POSIBLE, con momento central, rango y permanencia (horas)."""
    if not proy or not serie:
        return []
    f0 = max(serie)
    h0 = serie[f0]
    C, Mx, Mn = (_curva(f0, h0, proy, c) for c in ("central", "max", "min"))
    fin = C[-1][0]
    ev = []
    for nivel, nombre in ((alerta, "ALERTA"), (evac, "EVACUACION")):
        if h0 >= nivel:
            t = _cruce(C, nivel, -1)
            desde, antes = _desde_historia(serie, nivel)
            ev.append({"nivel": nombre, "tipo": "SALE", "umbral": nivel, "momento": t,
                       "desde_rango": _cruce(Mn, nivel, -1), "hasta_rango": _cruce(Mx, nivel, -1),
                       "en_nivel_desde": desde, "desde_antes_de_serie": antes,
                       "permanencia_h": ((t or fin) - desde).total_seconds() / HORA if desde else None,
                       "permanencia_minima": t is None or antes})
        else:
            t = _cruce(C, nivel, 1)
            if t is not None:
                sale = _cruce(C, nivel, -1, despues=t)
                ev.append({"nivel": nombre, "tipo": "ENTRA", "umbral": nivel, "momento": t,
                           "desde_rango": _cruce(Mx, nivel, 1), "hasta_rango": _cruce(Mn, nivel, 1),
                           "sale": sale, "permanencia_h": ((sale or fin) - t).total_seconds() / HORA,
                           "permanencia_minima": sale is None})
            else:
                tp = _cruce(Mx, nivel, 1)
                if tp is not None:
                    ev.append({"nivel": nombre, "tipo": "POSIBLE", "umbral": nivel, "momento": tp})
    return ev
