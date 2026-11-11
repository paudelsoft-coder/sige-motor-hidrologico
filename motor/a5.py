"""Cliente de la API pública del Sistema de Alerta del INA, alerta.ina.gob.ar/a5 (código propio del SIGE).

observaciones(serie, desde, hasta): alturas diarias de una serie del INA (escalas de Prefectura). Cada lectura se pasa
a la fecha argentina (UTC−3): la de las 03:00 UTC es la de las 00 h. En los puertos con marea hay varias lecturas por
día y se guarda la más alta.
"""
import csv
import datetime as dt
import io
import time
import urllib.parse
import urllib.request

RAIZ = "https://alerta.ina.gob.ar/a5/"
AGENTE = {"User-Agent": "SIGE-MotorHidrologico/2.0 (defensa civil)"}
ART = dt.timezone(dt.timedelta(hours=-3))


def pedir(metodo, parametros, intentos=3, espera=5):
    url = RAIZ + metodo + "?" + urllib.parse.urlencode(parametros)
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENTE), timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i + 1 == intentos:
                raise
            time.sleep(espera * (i + 1))


def observaciones(serie, desde, hasta, marea=False):
    """{fecha argentina: altura en m}."""
    texto = pedir("getObservaciones", {"tipo": "puntual", "series_id": serie, "timestart": desde, "timeend": hasta,
                                       "format": "csvless", "no_id": "true"})
    out = {}
    for fila in csv.reader(io.StringIO(texto)):
        if len(fila) < 3 or not fila[0][:4].isdigit():
            continue
        try:
            altura = float(fila[2])
            momento = dt.datetime.fromisoformat(fila[0].strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=dt.timezone.utc)
        dia = momento.astimezone(ART).date().isoformat()
        out[dia] = max(out[dia], altura) if marea and dia in out else altura
    return out
