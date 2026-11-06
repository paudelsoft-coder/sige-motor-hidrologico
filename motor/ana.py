"""Escalas de Brasil (ANA, Agência Nacional de Águas) en la cuenca del río Uruguay.

Dos servicios públicos del mismo servidor (telemetriaws1.ana.gov.br/ServiceANA.asmx):
  - HidroSerieHistorica: caudal diario consolidado (hoy llega hasta mediados de 2026).
  - DadosHidrometeorologicos: telemetría cada 15 minutos (nivel, caudal y lluvia) de los últimos días.
Se arma una serie diaria: histórica + promedio diario de la telemetría para los días que faltan.
"""
import datetime as dt
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

URL = "https://telemetriaws1.ana.gov.br/ServiceANA.asmx/"
UA = "SIGE-MotorHidrologico/1.0 (defensa civil; uso no comercial)"

# clave en el motor -> código ANA. Aguas arriba de lo que ya medimos con el INA (El Soberbio, Santo Tomé, P. de los Libres)
ESTACIONES = {
    "a_irai": "74100000",      # río Uruguay en Iraí (Brasil), ~1-2 días antes que El Soberbio
    "a_ijui": "75230000",      # río Ijuí en Santo Ângelo (afluente que entra entre El Soberbio y Garruchos)
    "a_ibicui": "76800000",    # río Ibicuí en Passo Mariano Pinto (gran afluente que entra antes de P. de los Libres)
    "a_alegrete": "76750000",  # río Ibirapuitã en Alegrete (afluente del Ibicuí)
    "a_sborja": "75780000",    # río Uruguay en Passo São Borja (frente a Santo Tomé)
}


def _get(metodo, params, timeout=90, intentos=3):
    url = URL + metodo + "?" + urllib.parse.urlencode(params)
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
                return ET.fromstring(r.read())
        except Exception:
            if i == intentos - 1:
                raise
            time.sleep(5 * (i + 1))


def _br(fecha):   # 2026-11-06 -> 06/11/2026
    return f"{fecha[8:10]}/{fecha[5:7]}/{fecha[:4]}"


def _hijos(nodo, nombre):
    return [e for e in nodo.iter() if e.tag.split("}")[-1] == nombre]


def _txt(e, nombre):
    for h in e:
        if h.tag.split("}")[-1] == nombre:
            return (h.text or "").strip()
    return ""


def historica(cod, desde, hasta):
    """Caudal diario consolidado {fecha: m³/s}. Si un mes está en bruto y consistido, se queda con el consistido."""
    raiz = _get("HidroSerieHistorica", {"codEstacao": cod, "dataInicio": _br(desde), "dataFim": _br(hasta),
                                        "tipoDados": 3, "nivelConsistencia": ""})
    out, nivel = {}, {}
    for e in _hijos(raiz, "SerieHistorica"):
        mes = _txt(e, "DataHora")[:8]
        niv = _txt(e, "NivelConsistencia")
        for d in range(1, 32):
            v = _txt(e, f"Vazao{d:02d}")
            if not v:
                continue
            f = mes + f"{d:02d}"
            if f not in out or niv == "2" and nivel.get(f) != "2":
                try:
                    out[f], nivel[f] = float(v), niv
                except ValueError:
                    pass
    return out


def telemetria(cod, desde, hasta):
    """Promedio diario del caudal telemétrico {fecha: m³/s}, pedido de a 3 días (el servicio se cuelga con rangos largos)."""
    suma = {}
    d0, fin = dt.date.fromisoformat(desde), dt.date.fromisoformat(hasta)
    while d0 <= fin:
        d1 = min(fin, d0 + dt.timedelta(days=2))
        try:
            raiz = _get("DadosHidrometeorologicos", {"codEstacao": cod, "dataInicio": _br(d0.isoformat()),
                                                     "dataFim": _br(d1.isoformat())}, timeout=60, intentos=2)
            for e in _hijos(raiz, "DadosHidrometereologicos"):
                f, v = _txt(e, "DataHora")[:10], _txt(e, "Vazao")
                try:
                    q = float(v)
                except ValueError:
                    continue
                if q > 0:
                    s = suma.setdefault(f, [0.0, 0])
                    s[0] += q
                    s[1] += 1
        except Exception as ex:
            print(f"ana: telemetría {cod} {d0}…{d1} sin datos ({str(ex)[:80]})")
        d0 = d1 + dt.timedelta(days=1)
        time.sleep(0.5)
    return {f: s[0] / s[1] for f, s in suma.items() if s[1] >= 8}   # al menos 2 horas de lecturas en el día


def series(desde, hasta=None, max_telemetria=150):
    """{clave: {fecha: caudal}} para todas las ESTACIONES (histórica + telemetría para lo que falte)."""
    hasta = hasta or dt.date.today().isoformat()
    out = {}
    for clave, cod in ESTACIONES.items():
        try:
            s = historica(cod, desde, hasta)
        except Exception as ex:
            print(f"ana: {clave} histórica sin datos ({str(ex)[:80]})")
            s = {}
        ultima = max(s) if s else desde
        ini = max((dt.date.fromisoformat(ultima) + dt.timedelta(days=1)),
                  dt.date.fromisoformat(hasta) - dt.timedelta(days=max_telemetria))
        if max_telemetria and ini.isoformat() <= hasta:
            s.update(telemetria(cod, ini.isoformat(), hasta))
        out[clave] = s
        print(f"ana: {clave} ({cod}) {len(s)} días" + (f", último {max(s)}" if s else ""))
    return out
