"""Alturas publicadas por Prefectura Naval Argentina (contenidosweb.prefecturanaval.gob.ar/alturas/).

Prefectura publica la tabla varias veces por día (lectura de las 00 h y, en muchos puertos, cada 2 horas). El INA la
replica después, una vez por día y con atraso. Por eso el motor lee las dos fuentes:
  - lectura de las 00 h  → sige_hidro_alturas (la misma serie diaria que usa el pronóstico; el INA la confirma después)
  - última lectura del día (cualquier hora) → sige_hidro_ultima (la "altura actual" que muestra el portal)
"""
import datetime as dt
import html
import re
import unicodedata
import urllib.request

URL = "https://contenidosweb.prefecturanaval.gob.ar/alturas/"
UA = "Mozilla/5.0 (SIGE-MotorHidrologico/1.0; defensa civil; uso no comercial)"
MESES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9,
         "OCT": 10, "NOV": 11, "DIC": 12}
# nombre en Prefectura (sin acentos, mayúsculas) [+ río] -> id de estación, cuando no coincide con nuestro nombre
ALIAS = {"IGUAZU": 9, "LIBERTADOR": 12, "PILCOMAYO": 55, "BARRANQUERAS": 20, "SAN LORENZO": 33, "PARANACITO": 43,
         "SALTO GRANDE ARRIBA": 77, "SALTO GRANDE ABAJO": 78, "BOCA GUALEGUAYCHU": 84, "SAN JAVIER|URUGUAY": 65}
IGNORAR = {"SAN JAVIER (SANTA FE)"}   # homónimo del San Javier del río Uruguay (no es una de nuestras estaciones)


def _norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9() ]", " ", s)).strip()


def _fecha_hora(txt):
    """'10/NOV/26 - 1000' -> ('2026-11-10', '10:00')."""
    m = re.match(r"(\d{1,2})/([A-Z]{3})/(\d{2})\s*-\s*(\d{2})(\d{2})", txt.strip().upper())
    if not m or m.group(2) not in MESES:
        return None, None
    f = dt.date(2000 + int(m.group(3)), MESES[m.group(2)], int(m.group(1))).isoformat()
    return f, f"{m.group(4)}:{m.group(5)}"


def _num(txt):
    try:
        return float(txt.strip().replace(",", "."))
    except ValueError:
        return None


def tabla():
    """Filas de la tabla: [{puerto, rio, altura, fecha, hora, altura_ant, fecha_ant, hora_ant}]."""
    req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept-Language": "es-AR,es"})
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read().decode("utf-8", "replace")
    filas = []
    limpio = lambda c: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", txt, flags=re.S | re.I):
        th = re.search(r"<th[^>]*>(.*?)</th>", tr, flags=re.S | re.I)
        if not th:
            continue
        c = {_norm(k): limpio(v) for k, v in re.findall(r'<td[^>]*data-label="([^"]*)"[^>]*>(.*?)</td>', tr, flags=re.S | re.I)}
        g = lambda *ks: next((c[k] for k in ks if k in c), "")
        f, h = _fecha_hora(g("FECHA HORA"))
        fa, ha = _fecha_hora(g("FECHA ANTERIOR"))
        filas.append({"puerto": limpio(th.group(1)), "rio": g("RIO"), "altura": _num(g("ULTIMO REGISTRO")), "fecha": f,
                      "hora": h, "altura_ant": _num(g("REGISTRO ANTERIOR")), "fecha_ant": fa, "hora_ant": ha})
    return filas


def lecturas(estaciones):
    """[{estacion_id, fecha, hora, altura_m}] de las estaciones del motor (sin las de marea, que usan el máximo diario
    del INA). Incluye la lectura actual y la anterior, así se recupera la de las 00 h aunque ya haya una más nueva."""
    por_nombre = {_norm(e["nombre"]): e["id"] for e in estaciones}
    marea = {e["id"] for e in estaciones if e.get("marea")}
    out = []
    for fila in tabla():
        n = _norm(fila["puerto"])
        if n in IGNORAR:
            continue
        eid = ALIAS.get(f"{n}|{_norm(fila['rio'])}") or ALIAS.get(n) or por_nombre.get(n)
        if not eid or eid in marea:
            continue
        for a, f, h in ((fila["altura"], fila["fecha"], fila["hora"]), (fila["altura_ant"], fila["fecha_ant"], fila["hora_ant"])):
            if a is not None and f and -5 < a < 400:
                out.append({"estacion_id": eid, "fecha": f, "hora": h, "altura_m": a})
    return out
