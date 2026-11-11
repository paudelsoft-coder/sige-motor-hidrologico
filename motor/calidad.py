"""Control de calidad de las alturas (código propio del SIGE).

Las alturas que publica el INA son las que carga Prefectura a mano: a veces hay ceros de registros vacíos, tipeos
(78,91 m en vez de 7,89 m) o picos de un solo día. `limpiar` los detecta con estadística robusta:
  1. escala del puerto  = percentil 98 de los cambios de un día (lo que el río cambia normalmente de un día al otro);
  2. ceros              = un 0 exacto cuando las lecturas de alrededor andan lejos de cero;
  3. filtro de Hampel   = lectura muy lejos de la mediana de las 7 anteriores y las 7 siguientes, salvo que el día
                          contiguo tenga un valor parecido (una crecida real dura más de un día);
  4. pico aislado       = sube (o baja) y al día siguiente vuelve donde estaba;
  5. último dato        = salto contra el anterior mayor que lo máximo creíble para ese puerto (`salto_max`).
`restituir` devuelve las lecturas altas descartadas que un puerto vecino confirma (crecidas muy rápidas).
"""
import numpy as np

from .base import entre, mover

VENTANA = 7


def _escala(fechas, v):
    d = [abs(b - a) for (fa, a), (fb, b) in zip(zip(fechas, v), zip(fechas[1:], v[1:])) if entre(fa, fb) == 1]
    return max(0.05, float(np.percentile(d, 98))) if len(d) > 30 else 0.3


def _mediana_movil(v, malas):
    """Mediana y desvío absoluto mediano de las VENTANA lecturas válidas a cada lado (sin la propia)."""
    x = np.where(malas, np.nan, v)
    pad = np.concatenate([np.full(VENTANA, np.nan), x, np.full(VENTANA, np.nan)])
    win = np.lib.stride_tricks.sliding_window_view(pad, 2 * VENTANA + 1).copy()
    win[:, VENTANA] = np.nan
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(win, axis=1)
            mad = np.nanmedian(np.abs(win - med[:, None]), axis=1)
            cuantos = np.sum(~np.isnan(win), axis=1)
    return med, mad, cuantos


def limpiar(serie, salto_max=None):
    """(serie_limpia, descartadas {fecha: valor})."""
    fechas = sorted(serie)
    if len(fechas) < 10:
        return dict(serie), {}
    v = np.array([serie[f] for f in fechas], dtype=float)
    n = len(v)
    esc = _escala(fechas, list(v))
    malas = np.zeros(n, bool)
    # ceros de registros vacíos
    med, _, cuantos = _mediana_movil(np.where(v == 0, np.nan, v), malas)
    malas |= (v == 0) & (np.abs(med) > max(1.0, 5 * esc)) & (cuantos >= 4)
    contiguo = np.array([0] + [entre(a, b) for a, b in zip(fechas, fechas[1:])])   # días desde la lectura anterior
    for _ in range(3):
        antes = malas.copy()
        med, mad, cuantos = _mediana_movil(v, malas)
        umbral = np.maximum(np.maximum(6 * 1.4826 * mad, 6 * esc), 0.5)
        lejos = (np.abs(v - med) > umbral) & (cuantos >= 6) & ~malas
        for i in np.nonzero(lejos)[0]:
            vecinos = [v[j] for j in (i - 1, i + 1) if 0 <= j < n and not malas[j]
                       and abs(entre(fechas[min(i, j)], fechas[max(i, j)])) == 1]
            dx = v[i] - med[i]
            # sostenido: un día contiguo también está lejos de la mediana, para el mismo lado (crecida real)
            sostenido = any((y - med[i]) * dx > 0 and abs(y - med[i]) > 0.4 * abs(dx) for y in vecinos)
            # tipeo: el valor es 3 veces (o un tercio de) lo normal, aunque el vecino también esté mal
            tipeo = abs(med[i]) > 0.5 and (v[i] / med[i] > 3 or v[i] / med[i] < 1 / 3)
            if not sostenido or tipeo:
                malas[i] = True
        # pico aislado: sale y vuelve
        idx = np.nonzero(~malas)[0]
        for a, i, c in zip(idx, idx[1:], idx[2:]):
            if entre(fechas[a], fechas[i]) > 2 or entre(fechas[i], fechas[c]) > 2:
                continue
            desvio = v[i] - (v[a] + v[c]) / 2
            if abs(desvio) > max(4 * esc, 0.3) and abs(v[c] - v[a]) < 0.25 * abs(desvio) and (v[i] - v[a]) * (v[i] - v[c]) > 0:
                malas[i] = True
        if (malas == antes).all():
            break
    idx = np.nonzero(~malas)[0]
    if salto_max and len(idx) > 1:
        a, b = idx[-2], idx[-1]
        if abs(v[b] - v[a]) > salto_max and entre(fechas[a], fechas[b]) <= 3:
            malas[b] = True
    descartadas = {fechas[i]: float(v[i]) for i in np.nonzero(malas)[0]}
    return {f: x for f, x in serie.items() if f not in descartadas}, descartadas


def salto_tipico(serie):
    """Mayor cambio de un día creíble para el puerto (percentil 99,9 de los cambios diarios × 1,5; mínimo 1 m)."""
    fechas = sorted(serie)
    d = [abs(serie[b] - serie[a]) for a, b in zip(fechas, fechas[1:]) if entre(a, b) == 1]
    if len(d) < 50:
        return None
    return round(max(1.0, 1.5 * float(np.percentile(d, 99.9))), 2)


def limpiar_dos_pasos(serie):
    """Limpieza general y, con lo aprendido del puerto, control del último dato."""
    l0, _ = limpiar(serie)
    return limpiar(serie, salto_tipico(l0))


def restituir(S, descartadas, estaciones, margen=5):
    """Devuelve a la serie las lecturas descartadas que son ≥ alerta (y < 1,6 × evacuación) cuando un puerto vecino del
    mismo río también estuvo en alerta dentro de ±`margen` días. Modifica S y descartadas; devuelve lo restituido."""
    por = {e["id"]: e for e in estaciones}
    rio = lambda e: "URUGUAY" if e["rio"].startswith("Uruguay") else "PARAGUAY" if e["rio"].startswith("Paraguay") else "PARANA"
    vuelven = {}
    for k, malas in descartadas.items():
        e = por.get(k)
        if not e or e.get("alerta") is None or not malas or k not in S:
            continue
        vecinos = sorted((u for u in estaciones if u["id"] != k and not u.get("marea") and u.get("alerta") is not None
                          and abs(u["orden"] - e["orden"]) <= 2.5 and (rio(u) == rio(e) or abs(u["orden"] - e["orden"]) < 1.2)),
                         key=lambda u: abs(u["orden"] - e["orden"]))[:4]
        for f, v in list(malas.items()):
            if v < e["alerta"] or v >= (e["evac"] or v) * 1.6:
                continue
            for u in vecinos:
                su, du = S.get(u["id"]) or {}, descartadas.get(u["id"]) or {}
                for d in range(-margen, margen + 1):
                    g = mover(f, d)
                    x = su.get(g, du.get(g))
                    if x is not None and x >= u["alerta"]:
                        S[k][f] = v
                        vuelven.setdefault(k, {})[f] = v
                        del malas[f]
                        break
                if f not in malas:
                    break
    return vuelven
