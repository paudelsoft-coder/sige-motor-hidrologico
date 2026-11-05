"""Descarga de alturas diarias desde la API pública del INA (Alerta Hidrológico Cuenca del Plata).

Endpoint: https://alerta.ina.gob.ar/a5/getObservaciones  (escalas de Prefectura Naval Argentina)
Adaptado de Vigía del Paraná · Required Notice: Copyright (c) 2026 Melisa Franzotti — Vigía del Paraná
"""
import datetime as dt
import time
import urllib.parse
import urllib.request

BASE = "https://alerta.ina.gob.ar/a5/getObservaciones"
UA = "SIGE-MotorHidrologico/1.0 (defensa civil; uso no comercial)"


def observaciones(serie_id: int, desde: str, hasta: str, marea: bool = False) -> dict:
    """{fecha (YYYY-MM-DD, día argentino): altura m}. Los registros de las 03:00Z son las 00 h de Argentina."""
    q = urllib.parse.urlencode({"tipo": "puntual", "series_id": serie_id, "timestart": desde,
                                "timeend": hasta, "format": "csvless", "no_id": "true"})
    req = urllib.request.Request(BASE + "?" + q, headers={"User-Agent": UA})
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read().decode("utf-8", "replace")
            break
        except Exception:
            if intento == 2:
                raise
            time.sleep(5 * (intento + 1))
    serie = {}
    for linea in txt.splitlines():
        p = linea.strip().split(",")
        if len(p) < 3 or not p[0][:4].isdigit():
            continue
        try:
            v = float(p[2])
        except ValueError:
            continue
        fecha = (dt.datetime.fromisoformat(p[0].replace("Z", "+00:00")) - dt.timedelta(hours=3)).date().isoformat()
        serie[fecha] = max(serie[fecha], v) if (marea and fecha in serie) else v
    return serie
