"""Proyección de alturas a 15 días y cruces de nivel (código propio del SIGE).

Modelo "sige2" (lo calibra modelo.py), uno por puerto:
  entradas = lista de diferencias {id, nombre, desde, hasta}: x = u(t − desde) − u(t − hasta) de una serie u
             (alturas de puertos, caudales de represas y ríos, lluvia acumulada; desde/hasta negativos = pronóstico)
  w[k]     = [b0, c1 … cn] para el horizonte k = 1..15:   h(t + k) = h(t) + b0 + Σ ci · xi
  err[k]   = [q10, q90] cuantiles 10 % y 90 % del error fuera de muestra → banda min / max
  err_sube = lo mismo medido solo en días de río subiendo (más de `sube_7d` m en 7 días)
Cruces: entre dos días la altura se interpola en línea recta; se busca el momento en que pasa la alerta o la
evacuación con el valor central (momento) y con los bordes de la banda (rango).
"""
import datetime as dt

from .base import UNA_HORA, medianoche, mover

H = 15
EXTERNAS = 1000     # ids ≥ 1000: Itaipú, represas, ríos de Brasil, lluvia (no son puertos)
MAX_FALTAN = 2      # series vecinas sin dato reciente que se toleran (se toman como "sin cambio")


def diferencia(serie, t, desde, hasta, tolerancia=3):
    """u(t − desde) − u(t − hasta). Si falta un extremo, prueba el mismo intervalo corrido hasta `tolerancia` días antes."""
    if not serie:
        return None
    for atras in range(tolerancia + 1):
        a, b = serie.get(mover(t, -desde - atras)), serie.get(mover(t, -hasta - atras))
        if a is not None and b is not None:
            return a - b
    return None


def es_sige2(M):
    return bool(M and M.get("w") and len(M["w"][0]) == len(M["entradas"]) + 1)


def es_lineal_simple(M):
    """Formato de transición (modelos calibrados antes de noviembre de 2026): w[k] = [c1 … cn] sin término fijo y
    banda simétrica err[k] = error típico. Se usa solo mientras un puerto no tenga modelo sige2."""
    return bool(M and M.get("w") and M.get("entradas") and len(M["w"][0]) == len(M["entradas"]))


def proyectar(series, modelo, est_id, sube_7d=0.15):
    """[{k, fecha, central, min, max, sube}] para el puerto, o None si no hay modelo o falta un dato de entrada."""
    M = modelo.get(str(est_id)) or modelo.get(est_id)
    s = series.get(est_id)
    simple = not es_sige2(M) and es_lineal_simple(M)
    if not (es_sige2(M) or simple) or not s:
        return None
    t = max(s)
    h0 = s[t]
    x, faltan = [], set()
    for en in M["entradas"]:
        k = int(en["id"])
        # represas, ríos de Brasil y lluvia se publican con días de atraso: se acepta el dato de hasta una semana antes
        v = diferencia(series.get(k), t, en["desde"], en["hasta"], tolerancia=7 if k >= EXTERNAS else 3)
        if v is None:
            if k == est_id:
                return None          # sin datos propios recientes no hay pronóstico
            faltan.add(k)
            v = 0.0                  # la serie vecina sin dato reciente se toma como "sin cambio"
        x.append(v)
    if len(faltan) > MAX_FALTAN:
        return None
    h7 = s.get(mover(t, -7))
    sube = h7 is not None and h0 - h7 > sube_7d and bool(M.get("err_sube"))
    bandas = M["err_sube"] if sube else M.get("err")
    out = []
    for k, fila in enumerate(M["w"], start=1):
        b0, c = (0.0, fila) if simple else (fila[0], fila[1:])
        central = h0 + b0 + sum(ci * xi for ci, xi in zip(c, x))
        q = bandas[k - 1] if bandas and k <= len(bandas) and bandas[k - 1] is not None else None
        if simple and q is None and M.get("err") and k <= len(M["err"]):
            q = M["err"][k - 1]
        if simple and q is not None:
            q = (-q, q)
        out.append({"k": k, "fecha": mover(t, k), "central": round(central, 2),
                    "min": round(central + q[0], 2) if q else None, "max": round(central + q[1], 2) if q else None,
                    "sube": sube})
    return out


# ------------------------------------------------------------------------------------------------- cruces de nivel
def _linea(f0, h0, proy, campo):
    return [(medianoche(f0), h0)] + [(medianoche(q["fecha"]), q[campo] if q.get(campo) is not None else q["central"])
                                     for q in proy]


def _pasa(linea, nivel, hacia_arriba, despues=None):
    """Primer momento en que la línea pasa el nivel (hacia arriba o hacia abajo), o None."""
    for (t1, y1), (t2, y2) in zip(linea, linea[1:]):
        if despues is not None and t2 <= despues:
            continue
        cruza = (y1 < nivel <= y2) if hacia_arriba else (y1 >= nivel > y2)
        if cruza:
            fraccion = (nivel - y1) / (y2 - y1)
            return (t1 + (t2 - t1) * fraccion).replace(second=0, microsecond=0)
    return None


def _desde_cuando(serie, nivel):
    """Desde cuándo el puerto está sobre el nivel (interpolado) y si eso empezó antes del primer dato."""
    fechas = sorted(serie)
    if serie[fechas[-1]] < nivel:
        return None, False
    i = len(fechas) - 1
    while i > 0 and serie[fechas[i - 1]] >= nivel:
        i -= 1
    if i == 0:
        return medianoche(fechas[0]), True
    a, b = fechas[i - 1], fechas[i]
    fraccion = (nivel - serie[a]) / (serie[b] - serie[a])
    return medianoche(a) + (medianoche(b) - medianoche(a)) * fraccion, False


def cruces(serie, proy, alerta, evac):
    """Eventos por nivel: ENTRA (cruza hacia arriba), SALE (ya está arriba y baja) o POSIBLE (solo el borde alto cruza)."""
    if not serie or not proy:
        return []
    f0 = max(serie)
    h0 = serie[f0]
    centro, alto, bajo = (_linea(f0, h0, proy, c) for c in ("central", "max", "min"))
    fin = centro[-1][0]
    horas = lambda a, b: (b - a).total_seconds() / UNA_HORA
    eventos = []
    for nivel, nombre in ((alerta, "ALERTA"), (evac, "EVACUACION")):
        if h0 >= nivel:
            baja = _pasa(centro, nivel, False)
            desde, antes = _desde_cuando(serie, nivel)
            eventos.append({"nivel": nombre, "tipo": "SALE", "umbral": nivel, "momento": baja,
                            "desde_rango": _pasa(bajo, nivel, False), "hasta_rango": _pasa(alto, nivel, False),
                            "en_nivel_desde": desde, "desde_antes_de_serie": antes,
                            "permanencia_h": horas(desde, baja or fin) if desde else None,
                            "permanencia_minima": baja is None or antes})
            continue
        entra = _pasa(centro, nivel, True)
        if entra is not None:
            sale = _pasa(centro, nivel, False, despues=entra)
            eventos.append({"nivel": nombre, "tipo": "ENTRA", "umbral": nivel, "momento": entra,
                            "desde_rango": _pasa(alto, nivel, True), "hasta_rango": _pasa(bajo, nivel, True),
                            "sale": sale, "permanencia_h": horas(entra, sale or fin), "permanencia_minima": sale is None})
        else:
            quizas = _pasa(alto, nivel, True)
            if quizas is not None:
                eventos.append({"nivel": nombre, "tipo": "POSIBLE", "umbral": nivel, "momento": quizas})
    return eventos
