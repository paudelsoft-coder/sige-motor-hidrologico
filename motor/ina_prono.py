"""Pronóstico oficial del INA por puerto, guardado cada día para compararlo con el nuestro.

Fuente: API pública del Sistema de Alerta del INA (alerta.ina.gob.ar/a5/getSeriesBySiteAndVar con includeProno=true).
Hoy publica pronóstico numérico para:
  - Paraná (modelo "tabprono_central"): Corrientes, Barranqueras, Goya, Reconquista, La Paz, Paraná, Santa Fe, Rosario
    → valores semanales a +7 y +14 días con rango inferior / medio / superior (se actualiza martes y viernes).
  - Paraguay ("tabprono_paraguay"): Puerto Pilcomayo, Formosa.
  - Delta (modelos horarios a 10 días): San Nicolás, Ramallo, Campana, San Fernando → se guarda un valor por día
    (00 h, o el máximo del día en los puertos con marea, igual que la serie que usa el motor).
Se guarda en sige_hidro_prono_ina; el nuestro, una vez por día, en sige_hidro_pron_archivo (ver __main__.pronostico).
"""
import datetime as dt
import json
import time
import urllib.parse
import urllib.request

URL = "https://alerta.ina.gob.ar/a5/getSeriesBySiteAndVar"
UA = "SIGE-MotorHidrologico/1.0 (defensa civil)"
ART = dt.timezone(dt.timedelta(hours=-3))


def _get(params):
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            if i == 2:
                raise
            time.sleep(5 * (i + 1))


def _dia(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ART)


def bajar(estaciones):
    """Filas {estacion_id, modelo, emitido, fecha, nivel, valor} de los pronósticos vigentes del INA."""
    hoy = dt.date.today()
    filas = []
    for e in estaciones:
        try:
            j = _get({"series_id": e["id"], "tipo": "puntual", "timestart": (hoy - dt.timedelta(days=10)).isoformat(),
                      "timeend": (hoy + dt.timedelta(days=30)).isoformat(), "includeProno": "true"})
        except Exception as ex:
            print(f"prono_ina: {e['nombre']} sin respuesta ({str(ex)[:60]})")
            continue
        for p in (j.get("pronosticos") or []) if isinstance(j, dict) else []:
            cor = p.get("corrida") or {}
            if not cor.get("forecast_date"):
                continue
            emitido = _dia(cor["forecast_date"]).date().isoformat()
            horario = (p.get("dt") or {}).get("hours") == 1
            for s in cor.get("series") or []:
                nivel = s.get("qualifier") or "medio"
                por_dia = {}
                for x in s.get("pronosticos") or []:
                    if x.get("valor") is None:
                        continue
                    t = _dia(x["timestart"])
                    f = t.date().isoformat()
                    if horario:
                        if e.get("marea"):
                            por_dia[f] = max(por_dia.get(f, -99), float(x["valor"]))
                        elif t.hour == 0:
                            por_dia[f] = float(x["valor"])
                    else:
                        por_dia[f] = float(x["valor"])
                filas += [{"estacion_id": e["id"], "modelo": p.get("nombre") or str(p.get("id")), "emitido": emitido,
                           "fecha": f, "nivel": nivel, "valor": round(v, 3)} for f, v in por_dia.items() if f > emitido]
        time.sleep(0.5)
    unicas = {(f["estacion_id"], f["modelo"], f["emitido"], f["fecha"], f["nivel"]): f for f in filas}   # sin repetidos
    return list(unicas.values())
