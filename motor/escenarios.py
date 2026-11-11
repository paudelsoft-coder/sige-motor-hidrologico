"""Pronóstico por escenarios de lluvia (código propio del SIGE).

El pronóstico normal usa un solo pronóstico de lluvia. Acá se usan los 51 escenarios del ensamble del ECMWF (IFS 0,25°,
15 días, Open-Meteo): para cada escenario se rearma la lluvia pronosticada de cada zona, se corre el mismo modelo de
cada puerto y se cuenta en cuántos escenarios el puerto llega a alerta o a evacuación.

Resultado por puerto (sige_hidro_escenarios, una fila por puerto y día del dato):
  prob_alerta / prob_evac = % de escenarios en que la altura central llega al nivel en los próximos 15 días
  dia_alerta / dia_evac   = día mediano de llegada entre los escenarios que llegan
  p10 / p50 / p90         = altura de cada día (1..15) en los escenarios: percentiles 10, 50 y 90
  usa_lluvia              = el modelo del puerto usa lluvia pronosticada (si no, todos los escenarios dan lo mismo)
"""
import datetime as dt
import json
import time
import urllib.parse
import urllib.request

import numpy as np

from . import om
from .base import mover
from .mejoras import ESCALA_LLUVIA, ZONAS
from .proyeccion import proyectar

MODELO = "ecmwf_ifs025"
URL = "https://ensemble-api.open-meteo.com/v1/ensemble?"


def _pedir(url, intentos=3):
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(om.firmar(url), headers={"User-Agent": "SIGE-Motor/2.0"}),
                                        timeout=120) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i + 1 == intentos:
                raise
            time.sleep(10 * (i + 1))


def bajar():
    """Lista de escenarios: [{id_zona: {fecha: mm}}], promedio de los puntos de cada zona. El escenario de control va
    primero. Los días sin dato en el ensamble (más allá de 15 días) no se incluyen."""
    pts = [(k, la, lo) for k, (_, _, P, _) in ZONAS.items() for la, lo in P]
    q = urllib.parse.urlencode({"latitude": ",".join(str(p[1]) for p in pts), "longitude": ",".join(str(p[2]) for p in pts),
                                "daily": "precipitation_sum", "models": MODELO, "forecast_days": 16,
                                "timezone": "America/Sao_Paulo"}, safe=",")
    j = _pedir(URL + q)
    j = j if isinstance(j, list) else [j]
    suma = {}   # {miembro: {zona: {fecha: [mm por punto]}}}
    for (k, _, _), d in zip(pts, j):
        dia = d["daily"]
        for clave, valores in dia.items():
            if not clave.startswith("precipitation_sum"):
                continue
            m = 0 if "_member" not in clave else int(clave.split("_member")[1][:2])
            for f, v in zip(dia["time"], valores):
                if v is not None and 0 <= v <= 400:
                    suma.setdefault(m, {}).setdefault(k, {}).setdefault(f, []).append(v)
    return [{k: {f: sum(v) / len(v) for f, v in Z.items()} for k, Z in suma[m].items()} for m in sorted(suma)]


def _con_escenario(S, escenario, desde):
    """Copia de S donde la lluvia acumulada de cada zona, desde `desde`, sigue la lluvia del escenario."""
    out = dict(S)
    for k, diaria in escenario.items():
        acc = S.get(k)
        if not acc:
            continue
        previo = mover(desde, -1)
        if previo not in acc:
            continue
        nueva = {f: v for f, v in acc.items() if f < desde}
        tot = acc[previo]
        f = desde
        while f in diaria:
            tot += diaria[f] / ESCALA_LLUVIA
            nueva[f] = round(tot, 4)
            f = mover(f, 1)
        # después del último día del ensamble sigue la lluvia del pronóstico único (para no cortar la serie)
        ult = mover(f, -1)
        if f in acc and ult in acc:
            base = nueva[ult] - acc[ult]
            g = f
            while g in acc:
                nueva[g] = round(acc[g] + base, 4)
                g = mover(g, 1)
        out[k] = nueva
    return out


def _primer_dia(proy, nivel):
    for q in proy:
        if q["central"] >= nivel:
            return q["fecha"]
    return None


def calcular(estaciones, S, modelo, sube_7d, escenarios, hoy=None):
    """Filas para sige_hidro_escenarios."""
    hoy = hoy or dt.date.today().isoformat()
    emitido = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec="minutes")
    lluvia = set(ZONAS)
    variantes = [_con_escenario(S, e, hoy) for e in escenarios]
    filas = []
    for e in estaciones:
        M = modelo.get(str(e["id"]))
        if not M or e.get("alerta") is None:
            continue
        usa = any(int(x["id"]) in lluvia and x["desde"] < 0 for x in M.get("entradas") or [])
        P = [proyectar(Sv, modelo, e["id"], sube_7d) for Sv in (variantes if usa else variantes[:1])]
        P = [p for p in P if p]
        if not P:
            continue
        C = np.array([[q["central"] for q in p] for p in P])
        fila = {"estacion_id": e["id"], "dato_fecha": max(S[e["id"]]), "emitido": emitido, "miembros": len(P),
                "usa_lluvia": usa, "p10": [round(float(x), 2) for x in np.percentile(C, 10, axis=0)],
                "p50": [round(float(x), 2) for x in np.percentile(C, 50, axis=0)],
                "p90": [round(float(x), 2) for x in np.percentile(C, 90, axis=0)]}
        for nivel, umbral in (("alerta", e["alerta"]), ("evac", e.get("evac"))):
            if umbral is None:
                fila[f"prob_{nivel}"], fila[f"dia_{nivel}"] = None, None
                continue
            dias = [_primer_dia(p, umbral) for p in P]
            llegan = sorted(d for d in dias if d)
            fila[f"prob_{nivel}"] = round(100 * len(llegan) / len(P))
            fila[f"dia_{nivel}"] = llegan[len(llegan) // 2] if llegan else None
        filas.append(fila)
    return filas
