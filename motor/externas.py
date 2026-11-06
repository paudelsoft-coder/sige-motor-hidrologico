"""Entradas externas al motor: caudal diario simulado por GloFAS en puntos de los ríos.

GloFAS (Global Flood Awareness System, Copernicus/ECMWF) simula el caudal de los ríos a partir de la lluvia
observada y pronosticada; además del pasado trae un pronóstico de 30 días. Lo usamos como "puertos virtuales"
aguas arriba (río Iguazú, Alto Uruguay en Brasil, río Ibicuí), donde no tenemos escalas del INA, y como
anticipo de lo que va a llegar.

Datos: Open-Meteo Flood API (https://open-meteo.com, CC BY 4.0), modelo GloFAS v4
© Copernicus Emergency Management Service. Uso no comercial gratuito (límite 10.000 llamadas por día).

Los puntos se eligieron buscando, cerca de cada lugar, la celda de 5 km con el mayor caudal (el cauce principal).
"""
import datetime as dt
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

URL = "https://flood-api.open-meteo.com/v1/flood"

# id: (nombre, lat, lon, cuenca)    ids 1100+ Paraná/Paraguay, 1200+ Uruguay
PUNTOS = {
    1101: ("GloFAS río Iguazú (Andresito)", -25.57, -54.30, "PARANA"),
    1102: ("GloFAS Paraná en Puerto Iguazú", -25.78, -54.67, "PARANA"),
    1103: ("GloFAS Paraná en Posadas", -27.35, -56.05, "PARANA"),
    1104: ("GloFAS río Paraguay inferior", -27.00, -58.55, "PARANA"),
    1105: ("GloFAS Paraná en Corrientes", -27.95, -58.85, "PARANA"),
    1201: ("GloFAS Uruguay en Iraí (Brasil)", -27.15, -53.50, "URUGUAY"),
    1202: ("GloFAS Uruguay en El Soberbio", -27.45, -54.40, "URUGUAY"),
    1203: ("GloFAS Uruguay en Santo Tomé", -28.75, -56.25, "URUGUAY"),
    1204: ("GloFAS río Ibicuí", -29.40, -56.80, "URUGUAY"),
    1205: ("GloFAS Uruguay en Paso de los Libres", -29.77, -57.29, "URUGUAY"),
    1206: ("GloFAS Uruguay en Concordia", -31.59, -58.02, "URUGUAY"),
}

PESO_MAX = 450   # Open-Meteo cuenta "peso" = lugares × días/14; el límite es 600 por minuto


def _pedir(ids, desde, hasta):
    q = {"latitude": ",".join(str(PUNTOS[i][1]) for i in ids),
         "longitude": ",".join(str(PUNTOS[i][2]) for i in ids),
         "daily": "river_discharge", "start_date": desde, "end_date": hasta}
    url = URL + "?" + urllib.parse.urlencode(q, safe=",")
    for intento in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sige-motor"}), timeout=120) as r:
                j = json.loads(r.read().decode())
            return j if isinstance(j, list) else [j]
        except urllib.error.HTTPError as ex:
            if ex.code == 429 and intento < 3:   # límite por minuto: esperar y reintentar
                time.sleep(65)
                continue
            raise RuntimeError(f"Open-Meteo {ex.code}: {ex.read().decode(errors='replace')[:200]}") from None


def caudales(desde: str, hasta: str, ids=None):
    """{id: {fecha: caudal en miles de m³/s}} entre `desde` y `hasta` (puede incluir fechas futuras: pronóstico)."""
    ids = list(ids or PUNTOS)
    dias = (dt.date.fromisoformat(hasta) - dt.date.fromisoformat(desde)).days + 1
    por_pedido = max(1, int(PESO_MAX / max(1.0, dias / 14)))
    out = {}
    for n, i0 in enumerate(range(0, len(ids), por_pedido)):
        grupo = ids[i0:i0 + por_pedido]
        if n:
            time.sleep(62)
        for k, d in zip(grupo, _pedir(grupo, desde, hasta)):
            dd = d.get("daily", {})
            out[k] = {f: round(v / 1000, 4) for f, v in zip(dd.get("time", []), dd.get("river_discharge", []))
                      if v is not None and not math.isnan(v)}
    return out


def extras():
    """Descripción para calibrar(): {id: {nombre, cuenca, futuro}}. `futuro` = puede usar el pronóstico de GloFAS."""
    return {k: {"nombre": v[0], "cuenca": v[3], "futuro": True} for k, v in PUNTOS.items()}
