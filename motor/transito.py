"""Tránsito de crecidas desde las represas (Itaipú → Yacyretá → puertos de aguas abajo).

Se calibra con datos históricos guardados en Supabase (tabla sige_hidro_represas y sige_hidro_alturas):
  1. desfase(): cuántos días tarda un cambio de caudal aguas arriba en notarse en la altura de un puerto,
     y cuántos cm sube el puerto por cada 1.000 m³/s (sensibilidad).
  2. celeridad(): velocidad de la onda de crecida = distancia del tramo / desfase.
  3. muskingum() / calibrar_muskingum(): tránsito del hidrograma por el tramo (amortigua y retrasa la onda).
  4. regla_vertedero(): caudal a partir del cual Yacyretá deja de turbinar todo y abre el vertedero.
  5. pulso(): "si Itaipú sube X m³/s, ¿cuándo llega y cuánto sube cada puerto?".
Las series son diarias. Con datos horarios (si se consiguen) las mismas funciones dan el resultado en horas
pasando dt_h = 1.
"""
from __future__ import annotations
import numpy as np


def _limpio(a):
    a = np.asarray(a, dtype=float)
    return a


def desfase(q_arriba, h_abajo, max_desfase: int = 20, min_puntos: int = 120):
    """Correlaciona Δcaudal(t) con Δaltura(t+d) para d = 0..max_desfase.
    Devuelve {desfase, correlacion, cm_por_1000} o None si no hay datos suficientes."""
    q, h = _limpio(q_arriba), _limpio(h_abajo)
    dq, dh = np.diff(q), np.diff(h)
    mejor = None
    for d in range(max_desfase + 1):
        a, b = dq[:len(dq) - d], dh[d:]
        m = ~np.isnan(a) & ~np.isnan(b)
        if m.sum() < min_puntos or a[m].std() == 0 or b[m].std() == 0:
            continue
        r = float(np.corrcoef(a[m], b[m])[0, 1])
        if mejor is None or r > mejor[1]:
            pend = float(np.polyfit(a[m], b[m], 1)[0])  # m de altura por m³/s
            mejor = (d, r, pend)
    if not mejor:
        return None
    d, r, pend = mejor
    return {"desfase": d, "correlacion": round(r, 3), "cm_por_1000": round(pend * 1000 * 100, 1)}


def celeridad(km_tramo: float, desfase_dias: float):
    """Velocidad de la onda en km/h (no es la velocidad del agua)."""
    if not km_tramo or not desfase_dias:
        return None
    return round(km_tramo / (desfase_dias * 24), 2)


def muskingum(entrada, K_h: float, X: float, dt_h: float = 24.0, salida0: float | None = None):
    """Tránsito Muskingum: O(t+1) = C0·I(t+1) + C1·I(t) + C2·O(t)."""
    I = _limpio(entrada)
    den = 2 * K_h * (1 - X) + dt_h
    C0 = (dt_h - 2 * K_h * X) / den
    C1 = (dt_h + 2 * K_h * X) / den
    C2 = (2 * K_h * (1 - X) - dt_h) / den
    O = np.empty_like(I)
    O[0] = I[0] if salida0 is None else salida0
    for t in range(len(I) - 1):
        O[t + 1] = C0 * I[t + 1] + C1 * I[t] + C2 * O[t]
    return O


def calibrar_muskingum(entrada, salida, dt_h: float = 24.0):
    """Busca K (horas) y X que mejor reproducen la salida observada (mínimo error cuadrático)."""
    I, O = _limpio(entrada), _limpio(salida)
    m = ~np.isnan(I) & ~np.isnan(O)
    if m.sum() < 60:
        return None
    I, O = I[m], O[m]
    mejor = None
    for K in np.arange(dt_h / 2, 24 * 15, dt_h / 4):
        for X in np.arange(0.0, 0.51, 0.05):
            if dt_h < 2 * K * X or dt_h > 2 * K * (1 - X):   # condición de estabilidad
                continue
            e = float(np.sqrt(np.mean((muskingum(I, K, X, dt_h, O[0]) - O) ** 2)))
            if mejor is None or e < mejor[2]:
                mejor = (float(K), float(X), e)
    return None if not mejor else {"K_h": mejor[0], "X": round(mejor[1], 2), "rmse_m3s": round(mejor[2], 1)}


def regla_vertedero(defluente, vertido, percentil: float = 95):
    """Caudal turbinable máximo estimado: percentil del caudal de salida en los días sin vertedero."""
    D, V = _limpio(defluente), _limpio(vertido)
    m = ~np.isnan(D) & ~np.isnan(V) & (V <= 1)
    if m.sum() < 30:
        return None
    return {"q_turbinable_max": round(float(np.percentile(D[m], percentil))),
            "dias_con_vertedero": int(((V > 1) & ~np.isnan(V)).sum())}


def pulso(delta_q: float, cadena: list[dict]):
    """Propaga un cambio de caudal por una cadena de tramos calibrados.
    cadena = [{"destino": "Posadas", "desfase": 3, "cm_por_1000": 18.0}, ...]  (desfase acumulado desde el origen)
    Devuelve [{"destino", "llega_en_dias", "sube_cm"}]."""
    return [{"destino": t["destino"], "llega_en_dias": t["desfase"],
             "sube_cm": round(delta_q / 1000 * t["cm_por_1000"], 1)} for t in cadena]
