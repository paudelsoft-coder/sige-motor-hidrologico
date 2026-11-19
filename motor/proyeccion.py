"""Proyección de alturas a 15 días y cruces de nivel (código propio del SIGE).

Modelo "sige2" (lo calibra modelo.py), uno por puerto:
  entradas = lista de diferencias {id, nombre, desde, hasta}: x = u(t − desde) − u(t − hasta) de una serie u
             (alturas de puertos, caudales de represas y ríos, lluvia acumulada; desde/hasta negativos = pronóstico)
  w[k]     = [b0, c1 … cn] para el horizonte k = 1..15:   h(t + k) = h(t) + b0 + Σ ci · xi
  err[k]   = [q10, q90] cuantiles 10 % y 90 % del error fuera de muestra → banda min / max
  err_sube = lo mismo medido solo en días de río subiendo (más de `sube_7d` m en 7 días)
  corto    = (opcional, desde nov. 2026) el mismo tipo de modelo calibrado SIN lluvia pronosticada: se usa para los
             horizontes 1..corto["k"]. La prueba fuera de muestra 2022-2026 mostró que a corto plazo las alturas de
             aguas arriba anticipan mejor que la lluvia futura (error a 1 día 17,7 cm contra 19,1 cm; 80 % de cruces
             detectados con 1 día de anticipación contra 72 %; 23 % de falsas alarmas contra 27 %).
Cruces: entre dos días la altura se une con una curva monótona (Fritsch-Carlson: no inventa picos ni bajadas que
los valores diarios no tienen) que usa también el día anterior, así respeta la aceleración de la crecida; se busca el
momento en que pasa la alerta o la evacuación con el valor central (momento) y con los bordes de la banda (rango).
Si Prefectura publicó una lectura posterior a la de las 00 h (cada 2-12 h en muchos puertos), la curva se ancla a
esa lectura: el desvío entre lo leído y lo pronosticado para esa hora se aplica al pronóstico y se va diluyendo.
"""
import datetime as dt
import math

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
    """[{k, fecha, central, min, max, sube}] para el puerto, o None si no hay modelo o falta un dato de entrada.
    Con modelo["corto"] los primeros corto["k"] días salen del modelo corto (sin lluvia pronosticada)."""
    M = modelo.get(str(est_id)) or modelo.get(est_id)
    largo = _proyectar(series, M, est_id, sube_7d)
    corto = M.get("corto") if M else None
    if not largo or not corto:
        return largo
    c = _proyectar(series, corto, est_id, sube_7d)
    if not c:
        return largo
    k_corto = int(corto.get("k") or 4)
    return [c[i] if q["k"] <= k_corto else q for i, q in enumerate(largo)]


def _proyectar(series, M, est_id, sube_7d=0.15):
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
DILUIR_H = 72.0    # el desvío de la última lectura de Prefectura se reduce a la mitad cada ~2 días (e^(−t/72 h))


def _linea(f0, h0, proy, campo, previo=None, ultima=None):
    """Puntos (momento, altura): [día anterior], 00 h del último dato, [lectura de Prefectura posterior], pronóstico.
    `ultima` = (momento, altura) de una lectura posterior a las 00 h: el pronóstico se corre lo que esa lectura se
    aparta de la curva en ese momento, y el corrimiento se diluye con el tiempo."""
    pts = ([previo] if previo else []) + [(medianoche(f0), h0)]
    fut = [(medianoche(q["fecha"]), q[campo] if q.get(campo) is not None else q["central"]) for q in proy]
    if ultima and fut and medianoche(f0) < ultima[0] < fut[0][0]:
        t_u, h_u = ultima
        esperado = _valor(pts + fut, t_u)
        desvio = h_u - esperado if esperado is not None else 0.0
        fut = [(t, y + desvio * math.exp(-(t - t_u).total_seconds() / 3600 / DILUIR_H)) for t, y in fut]
        pts = pts + [(t_u, h_u)]
    return pts + fut


def _pendientes(xs, ys):
    """Pendientes de Fritsch-Carlson (interpolación cúbica monótona)."""
    n = len(xs)
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2 for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        r = a * a + b * b
        if r > 9:
            t = 3 / math.sqrt(r)
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    return m


def _curva(linea):
    """[(x en horas, y)], pendientes."""
    t0 = linea[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in linea]
    ys = [y for _, y in linea]
    return t0, xs, ys, (_pendientes(xs, ys) if len(xs) >= 2 else [0.0] * len(xs))


def _hermite(x0, x1, y0, y1, m0, m1, x):
    h = x1 - x0
    t = (x - x0) / h
    return ((2 * t ** 3 - 3 * t ** 2 + 1) * y0 + (t ** 3 - 2 * t ** 2 + t) * h * m0
            + (-2 * t ** 3 + 3 * t ** 2) * y1 + (t ** 3 - t ** 2) * h * m1)


def _valor(linea, momento):
    """Altura de la curva en un momento (None fuera de rango)."""
    if len(linea) < 2:
        return None
    t0, xs, ys, m = _curva(linea)
    x = (momento - t0).total_seconds() / 3600
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            return _hermite(xs[i], xs[i + 1], ys[i], ys[i + 1], m[i], m[i + 1], x)
    return None


def _pasa(linea, nivel, hacia_arriba, despues=None):
    """Primer momento en que la curva pasa el nivel (hacia arriba o hacia abajo), o None. Se recorre cada tramo en
    pasos de 15 minutos sobre la curva monótona; un tramo sin cruce en sus extremos no puede cruzar (es monótona)."""
    if len(linea) < 2:
        return None
    t0, xs, ys, m = _curva(linea)
    for i in range(len(xs) - 1):
        t2 = t0 + dt.timedelta(hours=xs[i + 1])
        if despues is not None and t2 <= despues:
            continue
        y1, y2 = ys[i], ys[i + 1]
        cruza = (y1 < nivel <= y2) if hacia_arriba else (y1 >= nivel > y2)
        if not cruza:
            continue
        pasos = max(1, int(round((xs[i + 1] - xs[i]) * 4)))
        for j in range(1, pasos + 1):
            x = xs[i] + (xs[i + 1] - xs[i]) * j / pasos
            y = _hermite(xs[i], xs[i + 1], y1, y2, m[i], m[i + 1], x)
            if (y >= nivel) if hacia_arriba else (y < nivel):
                momento = t0 + dt.timedelta(hours=x)
                if despues is not None and momento <= despues:
                    continue
                return momento.replace(second=0, microsecond=0)
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


def cruces(serie, proy, alerta, evac, ultima=None):
    """Eventos por nivel: ENTRA (cruza hacia arriba), SALE (ya está arriba y baja) o POSIBLE (solo el borde alto cruza).
    `ultima` = (momento con zona horaria, altura) de la lectura de Prefectura más nueva, si es posterior a las 00 h."""
    if not serie or not proy:
        return []
    f0 = max(serie)
    h0 = serie[f0]
    ayer = mover(f0, -1)
    previo = (medianoche(ayer), serie[ayer]) if serie.get(ayer) is not None else None
    if ultima and not (medianoche(f0) < ultima[0] < medianoche(f0) + dt.timedelta(hours=36)):
        ultima = None
    centro, alto, bajo = (_linea(f0, h0, proy, c, previo, ultima) for c in ("central", "max", "min"))
    # el día anterior solo da la forma de la curva: los cruces se buscan desde las 00 h del último dato (o la lectura)
    if ultima:
        h0 = ultima[1]
    fin = centro[-1][0]
    horas = lambda a, b: (b - a).total_seconds() / UNA_HORA
    eventos = []
    for nivel, nombre in ((alerta, "ALERTA"), (evac, "EVACUACION")):
        if h0 >= nivel:
            ref = ultima[0] if ultima else medianoche(f0)
            baja = _pasa(centro, nivel, False, despues=ref)
            desde, antes = _desde_cuando(serie, nivel)
            if desde is None and ultima:   # pasó el nivel entre las 00 h y la última lectura de Prefectura
                desde = _pasa([(medianoche(f0), serie[f0]), ultima], nivel, True)
            eventos.append({"nivel": nombre, "tipo": "SALE", "umbral": nivel, "momento": baja,
                            "desde_rango": _pasa(bajo, nivel, False, despues=ref), "hasta_rango": _pasa(alto, nivel, False, despues=ref),
                            "en_nivel_desde": desde, "desde_antes_de_serie": antes,
                            "permanencia_h": horas(desde, baja or fin) if desde else None,
                            "permanencia_minima": baja is None or antes})
            continue
        inicio = ultima[0] if ultima else medianoche(f0)
        entra = _pasa(centro, nivel, True, despues=inicio)
        if entra is not None:
            sale = _pasa(centro, nivel, False, despues=entra)
            eventos.append({"nivel": nombre, "tipo": "ENTRA", "umbral": nivel, "momento": entra,
                            "desde_rango": _pasa(alto, nivel, True, despues=inicio), "hasta_rango": _pasa(bajo, nivel, True, despues=inicio),
                            "sale": sale, "permanencia_h": horas(entra, sale or fin), "permanencia_minima": sale is None})
        else:
            quizas = _pasa(alto, nivel, True, despues=inicio)
            if quizas is not None:
                eventos.append({"nivel": nombre, "tipo": "POSIBLE", "umbral": nivel, "momento": quizas})
    return eventos
