"""Modelo lluvia-caudal GR4J (Perrin, Michel y Andréassian, 2003) y tránsito simple, en Python puro.

Se usa para el caudal que aporta cada subcuenca del río Salado (datos/cuencas_salado.json). Los parámetros se
calibraron contra el caudal diario observado en Recreo (RP 70) — ver la nota de ese archivo — en lugar del coeficiente
de escorrentía fijo que se usa en las demás cuencas.

GR4J, paso diario, 4 parámetros:
  x1  capacidad del depósito de producción (mm): cuánta agua retiene el suelo antes de escurrir
  x2  intercambio con el acuífero / pérdidas (mm/día; negativo = la cuenca pierde agua: bañados, napas, riego)
  x3  capacidad del depósito de tránsito (mm)
  x4  base del hidrograma unitario (días): cuánto tarda en salir el agua de la propia subcuenca
Entradas: lluvia P y evapotranspiración potencial E (Hargreaves) en mm/día. Salida: caudal en mm/día → m³/s con el área.
Tránsito hasta el puerto: retardo = distancia / celeridad, más un embalse lineal de constante k días (bañados), y una
fracción que se pierde en el camino (el Salado santiagueño pierde la mayor parte de su agua antes de Santa Fe).
"""
import math


def _ss1(t, x4):
    if t <= 0:
        return 0.0
    return (t / x4) ** 2.5 if t < x4 else 1.0


def _ss2(t, x4):
    if t <= 0:
        return 0.0
    if t < x4:
        return 0.5 * (t / x4) ** 2.5
    if t < 2 * x4:
        return 1.0 - 0.5 * (2 - t / x4) ** 2.5
    return 1.0


def gr4j(P, E, x1, x2, x3, x4, s0=0.5, r0=0.4):
    """Listas de lluvia y ETP (mm/día) → (caudal mm/día, llenado del suelo S/x1) día por día."""
    nh1, nh2 = int(math.ceil(x4)) + 1, int(math.ceil(2 * x4)) + 1
    uh1 = [_ss1(i + 1, x4) - _ss1(i, x4) for i in range(nh1)]
    uh2 = [_ss2(i + 1, x4) - _ss2(i, x4) for i in range(nh2)]
    q1, q9 = [0.0] * nh1, [0.0] * nh2
    S, R = s0 * x1, r0 * x3
    Q, F_S = [], []
    for p, e in zip(P, E):
        p, e = p or 0.0, e or 0.0
        if p >= e:
            pn, ps = p - e, 0.0
            th = math.tanh(min(pn / x1, 13.0))
            ps = x1 * (1 - (S / x1) ** 2) * th / (1 + S / x1 * th)
            es = 0.0
        else:
            pn, ps = 0.0, 0.0
            th = math.tanh(min((e - p) / x1, 13.0))
            es = S * (2 - S / x1) * th / (1 + (1 - S / x1) * th)
        S = S - es + ps
        perc = S * (1 - (1 + (4.0 / 9.0 * S / x1) ** 4) ** -0.25)
        S -= perc
        pr = perc + pn - ps
        q1 = [q1[i + 1] + uh1[i] * pr * 0.9 for i in range(nh1 - 1)] + [uh1[-1] * pr * 0.9]
        q9 = [q9[i + 1] + uh2[i] * pr * 0.1 for i in range(nh2 - 1)] + [uh2[-1] * pr * 0.1]
        F = x2 * (R / x3) ** 3.5
        R = max(0.0, R + q1[0] + F)
        qr = R * (1 - (1 + (R / x3) ** 4) ** -0.25)
        R -= qr
        Q.append(qr + max(0.0, q9[0] + F))
        F_S.append(S / x1)
    return Q, F_S


def transitar(q, lag, k):
    """Retardo de `lag` días (fraccionario) y embalse lineal de `k` días."""
    L0 = int(math.floor(lag))
    fr = lag - L0
    a = 1.0 / (1.0 + k) if k > 0 else 1.0
    s, out = 0.0, []
    for t in range(len(q)):
        v = ((1 - fr) * q[t - L0] if t - L0 >= 0 else 0.0) + (fr * q[t - L0 - 1] if t - L0 - 1 >= 0 else 0.0)
        if k > 0:
            s += v
            o = a * s
            s -= o
            out.append(o)
        else:
            out.append(v)
    return out


def hargreaves(tmax, tmin, lat, dia_del_anio):
    """ETP de Hargreaves (mm/día)."""
    if tmax is None or tmin is None:
        return None
    phi = math.radians(lat)
    dr = 1 + 0.033 * math.cos(2 * math.pi * dia_del_anio / 365)
    de = 0.409 * math.sin(2 * math.pi * dia_del_anio / 365 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(de))))
    ra = 24 * 60 / math.pi * 0.082 * dr * (ws * math.sin(phi) * math.sin(de) + math.cos(phi) * math.cos(de) * math.sin(ws)) * 0.408
    return 0.0023 * ra * ((tmax + tmin) / 2 + 17.8) * math.sqrt(max(tmax - tmin, 0.0))
