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


# ---------------------------------------------------------------------------------------------------
# Control de calidad de las lecturas (el INA publica tal cual lo que carga Prefectura: a veces hay ceros
# de registros vacíos, valores intercambiados entre puertos o tipeos como 78,91 m en lugar de 7,89 m).
# ---------------------------------------------------------------------------------------------------
def _dias(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def depurar(serie, salto_max=None):
    """Quita lecturas imposibles y devuelve (serie_limpia, descartadas {fecha: valor}).
    R0 ceros exactos cuando el río anda lejos de cero · R1 pico de un día que sale y vuelve al mismo nivel ·
    R2 lectura muy lejos de la mediana de ±5 lecturas, salvo que el día contiguo la confirme (crecida real) ·
    último dato: salto contra el anterior mayor que `salto_max` (lo máximo creíble para ese puerto)."""
    f=sorted(serie)
    if len(f)<8: return dict(serie),{}
    v=[serie[x] for x in f]
    d=sorted(abs(b-a) for a,b in zip(v,v[1:]))
    mad=d[len(d)//2] or 0.01
    thr=max(0.8,10*mad)
    malas={}
    while True:
        idx=[i for i in range(len(f)) if f[i] not in malas]
        peor=None
        for j,i in enumerate(idx):
            x=v[i]
            w=[v[k] for k in idx[max(0,j-5):j]+idx[j+1:j+6]]
            # R0: cero exacto (registro vacío) cuando el río anda lejos de cero
            wn=[y for y in w if y!=0]
            if x==0 and wn and sorted(abs(y) for y in wn)[len(wn)//2]>thr: peor=(1e9,i);break
            if 0<j<len(idx)-1 and _dias(f[idx[j-1]],f[i])<=3 and _dias(f[i],f[idx[j+1]])<=3:
                a,c=v[idx[j-1]],v[idx[j+1]]
                dev=x-(a+c)/2
                # R1: pico de un día que sale y vuelve al mismo nivel
                if abs(dev)>thr and abs(c-a)<0.3*abs(dev) and (x-a)*(x-c)>0:
                    s=abs(dev)/thr
                    if not peor or s>peor[0]: peor=(s,i)
            # R2: lectura fuera del rango de sus vecinos (±5) por más del umbral
            m=sorted(w)[len(w)//2] if w else x
            if len(w)>=6 and abs(x-m)>3*thr:
                s=abs(x-m)/(3*thr)
                vec=[v[k] for k in (i-1,i+1) if 0<=k<len(f) and f[k] not in malas and abs(_dias(f[k],f[i]))==1]
                apoyado=any(abs(x-y)<=thr for y in vec)   # un vecino confirma el valor (crecida real de 2+ días)
                if (s>2 or not apoyado) and (not peor or s>peor[0]): peor=(s,i)
        if not peor: break
        malas[f[peor[1]]]=v[peor[1]]
    idx=[i for i in range(len(f)) if f[i] not in malas]
    if len(idx)>1:
        lim=salto_max if salto_max else max(1.0,15*mad)
        a,b=v[idx[-2]],v[idx[-1]]
        if abs(b-a)>lim and _dias(f[idx[-2]],f[idx[-1]])<=3: malas[f[idx[-1]]]=b
    return {k:x for k,x in serie.items() if k not in malas},malas
def salto_tipico(serie):
    """Mayor variación diaria creíble del puerto: percentil 99,9 de los cambios de un día (ya depurados) × 1,5."""
    f=sorted(serie);ch=sorted(abs(serie[b]-serie[a]) for a,b in zip(f,f[1:]) if _dias(a,b)==1)
    if len(ch)<50: return None
    return round(max(1.0,1.5*ch[min(len(ch)-1,int(len(ch)*0.999))]),2)
