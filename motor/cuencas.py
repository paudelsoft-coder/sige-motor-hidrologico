"""Lluvia caída y pronosticada, humedad del suelo y aporte estimado (m³/s) de cada cuenca del sistema del Plata.

Dos grupos de cuencas:
  · datos/cuencas.json      64 cuencas argentinas (COHIFE), unos pocos puntos por cuenca, 14 días de lluvia caída.
  · datos/cuencas_ext.json  23 cuencas de aporte FUERA de Argentina (Brasil, Paraguay, Bolivia, Uruguay; HydroBASINS),
                            malla regular de 0,5° (757 puntos), 30 días de lluvia caída y humedad del suelo en 3 capas.
                            Cada una indica a qué puerto argentino llega su agua y en cuántos días (viaje_min..viaje_max).

Fuente: Open-Meteo (pronóstico 16 días + días pasados, modelos meteorológicos combinados). Para cada cuenca se
promedian los puntos dentro del polígono.

Humedad del suelo (m³/m³ de agua por m³ de suelo) → saturación S = (θ − 0,10) / (0,42 − 0,10), recortada a 0..1:
    suelo       capa 9-27 cm  (zona de raíces: decide cuánto de la lluvia escurre)
    suelo_sup   capa 3-9 cm   (solo cuencas de afuera: responde en horas a la lluvia)
    suelo_prof  capa 27-81 cm (solo cuencas de afuera: memoria de semanas; suelo profundo saturado = crecida más larga)

Aporte estimado (método simple de coeficiente de escorrentía, todavía SIN calibrar contra los puertos):
    lluvia útil del día = max(0, P − 5 mm · (1 − S))       (con suelo seco los primeros mm se infiltran)
    coeficiente C = 0,03 + 0,42 · S²                       (3 % con suelo seco … 45 % con suelo saturado)
    aporte = Σ C · lluvia útil · área / (días · 86 400 s)  → m³/s medios del período
    aporte_real_m3s   con la lluvia caída en la ventana de la cuenca (ventana_dias: 3, 7, 14 o 30 días)
    aporte_7d_m3s     con la lluvia caída en los últimos 7 días
    aporte_pron7d_m3s con la lluvia pronosticada para los próximos 7 días
Es un orden de magnitud para comparar cuencas y ver qué viene; el motor solo lo usará para pronosticar alturas si
una prueba hacia atrás muestra que baja el error.

Ventanas y tiempos de viaje (análisis ERA5 2009-2026 contra alturas diarias del INA, correlación lluvia → subida):
    Uruguay alto / medio / Ibicuí → Paso de los Libres / Monte Caseros: el agua llega en 2-6 días; anticipa mejor
        la lluvia de los últimos 3 días.
    Iguazú → Andresito: 0-2 días.
    Alto Paraná (Brasil) → Corrientes: 1-11 días (lluvia de 7 días); → Paraná: 6-22 días (lluvia de 14 días).
    Cuenca del Paraguay → Formosa: 0-16 días, pico a los 9 (lluvia de 14 días).
    Pantanal: semanas a meses (no se ve en la correlación diaria; manda la humedad profunda del suelo y 30 días).
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
CAPAS_EXT = ("soil_moisture_3_to_9cm", "soil_moisture_9_to_27cm", "soil_moisture_27_to_81cm")


def cuencas():
    return json.load(open(os.path.join(RAIZ, "datos", "cuencas.json"), encoding="utf-8"))["cuencas"]


def cuencas_ext():
    p = os.path.join(RAIZ, "datos", "cuencas_ext.json")
    return json.load(open(p, encoding="utf-8"))["cuencas"] if os.path.exists(p) else []


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


def _bajar(puntos, pasados=14, capas=("soil_moisture_9_to_27cm",)):
    """[(lat, lng)] → [{fecha: (mm, {capa: θ medio del día})}] en el mismo orden."""
    out = []
    for i in range(0, len(puntos), LOTE):
        p = puntos[i:i + LOTE]
        q = {"latitude": ",".join(str(a) for a, _ in p), "longitude": ",".join(str(b) for _, b in p),
             "daily": "precipitation_sum", "hourly": ",".join(capas), "past_days": pasados, "forecast_days": 16,
             "timezone": "America/Argentina/Buenos_Aires"}
        r = _get(URL + "?" + urllib.parse.urlencode(q))
        for x in (r if isinstance(r, list) else [r]):
            th = {}
            for capa in capas:
                for t, v in zip(x["hourly"]["time"], x["hourly"].get(capa) or []):
                    if v is not None:
                        th.setdefault(t[:10], {}).setdefault(capa, []).append(v)
            d = {}
            for f, mm in zip(x["daily"]["time"], x["daily"]["precipitation_sum"]):
                d[f] = (mm, {c: sum(v) / len(v) for c, v in th.get(f, {}).items() if v})
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


def _media(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fila(c, mios, hoy, ext):
    """Promedia los puntos de una cuenca y arma la fila de sige_hidro_cuencas."""
    raiz = "soil_moisture_9_to_27cm"
    fechas = sorted({f for d in mios for f in d})
    serie = []   # (fecha, mm, θ raíces, θ superficial, θ profundo)
    for f in fechas:
        mm = _media([d[f][0] for d in mios if f in d])
        capa = lambda k: _media([d[f][1].get(k) for d in mios if f in d])
        serie.append((f, mm, capa(raiz), capa("soil_moisture_3_to_9cm"), capa("soil_moisture_27_to_81cm")))
    pas = [x for x in serie if x[0] < hoy]
    fut = [x for x in serie if x[0] >= hoy]
    suma = lambda xs: round(sum(x[1] or 0 for x in xs), 1)
    ultimo = lambda k: next((x[k] for x in reversed(pas) if x[k] is not None), None)
    dias = lambda xs: [(x[1], _sat(x[2])) for x in xs]
    sat = lambda th: round(_sat(th), 2) if th is not None else None
    ventana = int(c.get("ventana_dias") or 7)
    hay_puerto = bool(c.get("puerto_id"))
    fila = {
        "cuenca": c["id"], "nombre": c["nombre"], "subsistema": c["subsistema"], "puerto_id": c.get("puerto_id"),
        "area_km2": c["area_km2"], "pais": c.get("pais") or "Argentina",
        "lluvia_3d": suma(pas[-3:]), "lluvia_7d": suma(pas[-7:]), "lluvia_14d": suma(pas[-14:]),
        "lluvia_30d": suma(pas[-30:]) if ext else None,
        "pron_3d": suma(fut[:3]), "pron_7d": suma(fut[:7]), "pron_16d": suma(fut[:16]),
        "suelo": sat(ultimo(2)), "suelo_sup": sat(ultimo(3)) if ext else None, "suelo_prof": sat(ultimo(4)) if ext else None,
        "aporte_7d_m3s": round(_aporte(dias(pas[-7:]), c["area_km2"])) if hay_puerto else None,
        "aporte_pron7d_m3s": round(_aporte(dias(fut[:7]), c["area_km2"])) if hay_puerto else None,
        "aporte_real_m3s": round(_aporte(dias(pas[-ventana:]), c["area_km2"])) if hay_puerto else None,
        "ventana_dias": ventana, "viaje_min": c.get("viaje_min"), "viaje_max": c.get("viaje_max"),
        "puntos": len(c["puntos"]),
        "serie": [dict({"f": f, "mm": round(mm, 1) if mm is not None else None, "s": sat(th)},
                       **({"ss": sat(ts), "sp": sat(tp)} if ext else {}))
                  for f, mm, th, ts, tp in serie],
        "actualizado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
    }
    return fila


def calcular(hoy=None):
    hoy = hoy or dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date().isoformat()
    filas = []
    for grupo, ext in ((cuencas(), False), (cuencas_ext(), True)):
        if not grupo:
            continue
        puntos = [tuple(p) for c in grupo for p in c["puntos"]]
        try:
            datos = _bajar(puntos, pasados=30, capas=CAPAS_EXT) if ext else _bajar(puntos)
        except Exception as ex:
            if not ext:
                raise
            # si Open-Meteo falla con las cuencas de afuera, las argentinas igual se guardan
            print("cuencas de afuera: sin actualizar:", ex)
            continue
        j = 0
        for c in grupo:
            mios = datos[j:j + len(c["puntos"])]
            j += len(c["puntos"])
            filas.append(_fila(c, mios, hoy, ext))
    return filas
