"""Lluvia caída y pronosticada, humedad del suelo y aporte estimado (m³/s) de cada cuenca argentina del sistema del Plata.

Fuente: Open-Meteo (pronóstico 16 días + últimos 14 días, modelos meteorológicos combinados). Para cada cuenca se
promedian varios puntos dentro del polígono (datos/cuencas.json).

Aporte estimado (método simple de coeficiente de escorrentía, todavía SIN calibrar contra los puertos):
    saturación S = (θ − 0,10) / (0,42 − 0,10)  con θ = humedad del suelo 9-27 cm (m³/m³), recortada a 0..1
    lluvia útil del día = max(0, P − 5 mm · (1 − S))       (con suelo seco los primeros mm se infiltran)
    coeficiente C = 0,03 + 0,42 · S²                       (3 % con suelo seco … 45 % con suelo saturado)
    aporte = Σ C · lluvia útil · área / (días · 86 400 s)  → m³/s medios del período
Es un orden de magnitud para comparar cuencas y ver qué viene; el motor solo lo usará para pronosticar alturas si
una prueba hacia atrás muestra que baja el error.
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import om

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://api.open-meteo.com/v1/forecast"
LOTE = 50
TH_SECO, TH_SAT = 0.10, 0.42


def cuencas():
    return json.load(open(os.path.join(RAIZ, "datos", "cuencas.json"), encoding="utf-8"))["cuencas"]


def _get(url, intentos=5):
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(om.firmar(url), headers={"User-Agent": "sige-motor"}), timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as ex:
            if ex.code in (429, 502, 503) and i < intentos - 1:
                time.sleep(60)
                continue
            raise
        except urllib.error.URLError:
            if i < intentos - 1:
                time.sleep(20)
                continue
            raise


def _bajar(puntos):
    """[(lat, lng)] → [{fecha: (mm, θ medio del día)}] en el mismo orden."""
    out = []
    for i in range(0, len(puntos), LOTE):
        p = puntos[i:i + LOTE]
        q = {"latitude": ",".join(str(a) for a, _ in p), "longitude": ",".join(str(b) for _, b in p),
             "daily": "precipitation_sum", "hourly": "soil_moisture_9_to_27cm", "past_days": 14, "forecast_days": 16,
             "timezone": "America/Argentina/Buenos_Aires"}
        r = _get(URL + "?" + urllib.parse.urlencode(q))
        for x in (r if isinstance(r, list) else [r]):
            d = {}
            th = {}
            for t, v in zip(x["hourly"]["time"], x["hourly"]["soil_moisture_9_to_27cm"]):
                if v is not None:
                    th.setdefault(t[:10], []).append(v)
            for f, mm in zip(x["daily"]["time"], x["daily"]["precipitation_sum"]):
                d[f] = (mm, sum(th[f]) / len(th[f]) if th.get(f) else None)
            out.append(d)
        time.sleep(2)
    return out


def _sat(th):
    return None if th is None else max(0.0, min(1.0, (th - TH_SECO) / (TH_SAT - TH_SECO)))


def _aporte(dias, area):
    """dias = [(mm, S)] → m³/s medios."""
    vol = 0.0
    for mm, s in dias:
        if mm is None:
            continue
        s = 0.5 if s is None else s
        util = max(0.0, mm - 5 * (1 - s))
        vol += (0.03 + 0.42 * s * s) * util / 1000 * area * 1e6
    return vol / (max(1, len(dias)) * 86400)


def calcular(hoy=None):
    hoy = hoy or dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date().isoformat()
    C = cuencas()
    puntos = [tuple(p) for c in C for p in c["puntos"]]
    datos = _bajar(puntos)
    filas, j = [], 0
    for c in C:
        mios = datos[j:j + len(c["puntos"])]
        j += len(c["puntos"])
        fechas = sorted({f for d in mios for f in d})
        serie = []
        for f in fechas:
            mm = [d[f][0] for d in mios if f in d and d[f][0] is not None]
            th = [d[f][1] for d in mios if f in d and d[f][1] is not None]
            serie.append((f, sum(mm) / len(mm) if mm else None, sum(th) / len(th) if th else None))
        pas = [x for x in serie if x[0] < hoy]
        fut = [x for x in serie if x[0] >= hoy]
        suma = lambda xs: round(sum(x[1] or 0 for x in xs), 1)
        hoy_th = next((x[2] for x in reversed(pas) if x[2] is not None), None)
        dias = lambda xs: [(x[1], _sat(x[2])) for x in xs]
        filas.append({
            "cuenca": c["id"], "nombre": c["nombre"], "subsistema": c["subsistema"], "puerto_id": c["puerto_id"],
            "area_km2": c["area_km2"],
            "lluvia_3d": suma(pas[-3:]), "lluvia_7d": suma(pas[-7:]), "lluvia_14d": suma(pas[-14:]),
            "pron_3d": suma(fut[:3]), "pron_7d": suma(fut[:7]), "pron_16d": suma(fut[:16]),
            "suelo": round(_sat(hoy_th), 2) if hoy_th is not None else None,
            "aporte_7d_m3s": round(_aporte(dias(pas[-7:]), c["area_km2"])) if c["puerto_id"] else None,
            "aporte_pron7d_m3s": round(_aporte(dias(fut[:7]), c["area_km2"])) if c["puerto_id"] else None,
            "serie": [{"f": f, "mm": round(mm, 1) if mm is not None else None, "s": round(_sat(th), 2) if th is not None else None}
                      for f, mm, th in serie],
            "actualizado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        })
    return filas
