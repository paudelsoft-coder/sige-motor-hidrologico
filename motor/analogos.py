"""Situaciones parecidas en la historia (código propio del SIGE).

Para la altura de hoy, su cambio en la última semana y la época del año, busca los días del pasado más parecidos
(distancia con las tres cosas en escalas comparables), toma hasta `n` casos separados por más de 30 días y resume
qué pasó en los 30 días siguientes: cuánto subió, dónde terminó y cuántos días tardó en entrar o salir de alerta y
evacuación. Necesita al menos 10 años de historia.
"""
import datetime as dt

import numpy as np

from .base import mover, valor_cercano


def _dia_del_anio(f):
    return dt.date.fromisoformat(f).timetuple().tm_yday


def _separacion_doy(a, b):
    d = abs(a - b)
    return min(d, 365 - d)


def _q(valores, p):
    v = [x for x in valores if x is not None]
    return float(np.percentile(v, p * 100, method="nearest")) if v else None


def analogos(serie, alerta, evac, n=10, horizonte=30):
    """(situacion, resumen, casos) o None si hay menos de 10 años de datos."""
    if len(serie) < 3650:
        return None
    hoy = max(serie)
    h0 = serie[hoy]
    hace7 = valor_cercano(serie, mover(hoy, -7))
    if hace7 is None:
        return None
    c0 = h0 - hace7
    situacion = {"f": hoy, "h": h0, "c7": round(c0, 2)}
    todos = np.array(list(serie.values()))
    rango = max(0.5, float(np.percentile(todos, 95) - np.percentile(todos, 5)))
    esc_h, esc_c = max(0.10, 0.04 * rango), max(0.08, 0.03 * rango)
    doy0 = _dia_del_anio(hoy)
    ultimo_util = mover(hoy, -horizonte - 5)
    candidatos = []
    for f, h in serie.items():
        if f > ultimo_util or abs(h - h0) > 2.5 * esc_h:
            continue
        a = valor_cercano(serie, mover(f, -7))
        if a is None or abs((h - a) - c0) > 2.5 * esc_c:
            continue
        dist = ((h - h0) / esc_h) ** 2 + ((h - a - c0) / esc_c) ** 2 + 0.5 * (_separacion_doy(_dia_del_anio(f), doy0) / 60) ** 2
        candidatos.append((dist, f, h, h - a))
    candidatos.sort()
    estado = lambda x: 2 if x >= evac else 1 if x >= alerta else 0
    mismos = [c for c in candidatos if estado(c[2]) == estado(h0)]
    if len(mismos) >= 30:
        candidatos = mismos
    elegidos = []
    for c in candidatos:
        if any(abs((dt.date.fromisoformat(c[1]) - dt.date.fromisoformat(g[1])).days) <= 30 for g in elegidos):
            continue
        despues = [serie.get(mover(c[1], k)) for k in range(1, horizonte + 1)]
        if sum(x is not None for x in despues) >= 0.7 * horizonte:
            elegidos.append(c)
        if len(elegidos) >= n:
            break
    if len(elegidos) < 3:
        return situacion, {"n": len(elegidos)}, []
    casos = []
    for _, f, h, c in elegidos:
        fut = [(k, serie[mover(f, k)]) for k in range(1, horizonte + 1) if mover(f, k) in serie]
        primero = lambda cond: next((k for k, x in fut if cond(x)), None)
        casos.append({"f": f, "h": round(h, 2), "c7": round(c, 2),
                      "max30": round(max(x for _, x in fut), 2), "min30": round(min(x for _, x in fut), 2),
                      "fin30": round(fut[-1][1], 2),
                      "baja_alerta": primero(lambda x: x < alerta) if h >= alerta else None,
                      "baja_evac": primero(lambda x: x < evac) if h >= evac else None,
                      "entra_alerta": primero(lambda x: x >= alerta) if h < alerta else None,
                      "entra_evac": primero(lambda x: x >= evac) if h < evac else None})
    misma_epoca = [h for f, h in serie.items() if f < hoy and _separacion_doy(_dia_del_anio(f), doy0) <= 15]
    rareza = round(100 * sum(1 for h in misma_epoca if h < h0) / len(misma_epoca)) if misma_epoca else None

    def reparto(campo):
        nivel = alerta if "alerta" in campo else evac
        aplica = [c for c in casos if (c["h"] >= nivel) == campo.startswith("baja")]
        v = [c[campo] for c in casos if c[campo] is not None]
        return {"n": len(aplica), "si": len(v), "p25": _q(v, .25), "p50": _q(v, .5), "p75": _q(v, .75)}

    subida = [c["max30"] - c["h"] for c in casos]
    resumen = {"n": len(casos), "desde": min(serie)[:4], "rareza_pct": rareza,
               "suben": sum(1 for c in casos if c["max30"] > c["h"] + 0.2),
               "max30_p50": round(_q(subida, .5), 2), "max30_p90": round(_q(subida, .9), 2),
               "fin30_p50": round(_q([c["fin30"] - c["h"] for c in casos], .5), 2),
               "baja_alerta": reparto("baja_alerta"), "baja_evac": reparto("baja_evac"),
               "entra_alerta": reparto("entra_alerta"), "entra_evac": reparto("entra_evac")}
    return situacion, resumen, casos
