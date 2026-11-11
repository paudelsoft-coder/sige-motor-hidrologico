"""Entradas nuevas para el pronóstico de alturas: lo que pasa en Brasil y Paraguay antes de llegar a nuestros puertos.

  - Represas (ONS, Brasil): caudal que sueltan Baixo Iguaçu y Salto Caxias (río Iguazú), Porto Primavera (Paraná),
    Foz do Chapecó e Itá (Alto Uruguay). Salto Grande (INA/CTM): lo que entra y lo que suelta.
  - Ríos de Brasil (ANA): Iguazú en União da Vitória; Uruguay en Iraí y São Borja; Ijuí, Ibicuí e Ibirapuitã.
  - Lluvia por zona (ERA5 de Open-Meteo para el pasado; modelo meteorológico para los últimos días y el pronóstico),
    guardada como lluvia ACUMULADA: así la diferencia entre dos días es la lluvia caída (o pronosticada) en ese lapso,
    y entra al modelo con las mismas ventanas que las alturas (últimos 3 / 7 días, semana anterior, próximos 3/7/14).
Ids ficticios: 1001-1014 represas · 1021-1026 ANA · 1031-1045 lluvia.  Unidades: miles de m³/s y metros de lluvia/10.
"""
import datetime as dt
import time
import urllib.parse

# Ids por represa (sige_hidro_represas): (clave, campo, cuenca, nombre)
REPRESAS = {1001: ("BAIXO_IGUACU", "defluente_m3s", "PARANA", "Represa Baixo Iguaçu (río Iguazú)"),
            1002: ("SALTO_CAXIAS", "defluente_m3s", "PARANA", "Represa Salto Caxias (río Iguazú)"),
            1003: ("PORTO_PRIMAVERA", "defluente_m3s", "PARANA", "Represa Porto Primavera (Paraná)"),
            1011: ("FOZ_CHAPECO", "defluente_m3s", "URUGUAY", "Represa Foz do Chapecó (Alto Uruguay)"),
            1012: ("ITA", "defluente_m3s", "URUGUAY", "Represa Itá (Alto Uruguay)"),
            1013: ("SALTO_GRANDE", "afluente_m3s", "URUGUAY", "Salto Grande: caudal que entra"),
            1014: ("SALTO_GRANDE", "defluente_m3s", "URUGUAY", "Salto Grande: caudal que suelta")}
ANA = {1021: ("74100000", "URUGUAY", "ANA río Uruguay en Iraí"),
       1022: ("75230000", "URUGUAY", "ANA río Ijuí en Santo Ângelo"),
       1023: ("76800000", "URUGUAY", "ANA río Ibicuí"),
       1024: ("76750000", "URUGUAY", "ANA río Ibirapuitã en Alegrete"),
       1025: ("75780000", "URUGUAY", "ANA río Uruguay en São Borja"),
       1026: ("65310000", "PARANA", "ANA río Iguazú en União da Vitória")}
# Zonas de lluvia: id -> (nombre en sige_hidro_lluvia, cuenca, puntos [lat, lon], nombre)
ZONAS = {1031: ("alto", "URUGUAY", [(-27, -52), (-27.8, -51)], "Lluvia Alto Uruguay (Santa Catarina / RS)"),
         1032: ("medio", "URUGUAY", [(-28, -54), (-28.5, -55)], "Lluvia Uruguay medio (RS / Misiones)"),
         1033: ("ibicui", "URUGUAY", [(-29.5, -54.5), (-29.8, -55.8)], "Lluvia cuenca del Ibicuí"),
         1034: ("corr", "URUGUAY", [(-28.5, -56.5), (-29.5, -57.5)], "Lluvia Corrientes este"),
         1041: ("iguazu", "PARANA", [(-25.9, -51.0), (-26.2, -49.8), (-25.6, -52.6)], "Lluvia cuenca del Iguazú (Paraná, BR)"),
         1042: ("parana_br", "PARANA", [(-23.8, -52.5), (-22.8, -51.5), (-24.6, -53.6)], "Lluvia Paraná brasileño (Ivaí / Paranapanema)"),
         1043: ("paraguay", "PARANA", [(-25.5, -57.0), (-24.0, -56.5)], "Lluvia Paraguay oriental"),
         1044: ("nea", "PARANA", [(-27.5, -58.5), (-28.5, -59.5), (-29.5, -58.3)], "Lluvia Corrientes / Chaco"),
         1045: ("litoral", "PARANA", [(-31.0, -60.5), (-31.5, -59.5)], "Lluvia Santa Fe / Entre Ríos")}
ESCALA_LLUVIA = 100.0   # mm → décimas de metro: 100 mm acumulados = 1 unidad (del orden de las alturas)


def meta():
    """{id: {nombre, cuenca, tipo}} de todas las entradas nuevas."""
    m = {k: {"nombre": v[3], "cuenca": v[2], "tipo": "represa"} for k, v in REPRESAS.items()}
    m.update({k: {"nombre": v[2], "cuenca": v[1], "tipo": "ana"} for k, v in ANA.items()})
    m.update({k: {"nombre": v[3], "cuenca": v[1], "tipo": "lluvia"} for k, v in ZONAS.items()})
    return m


def _represas(desde):
    from . import supa
    claves = sorted({v[0] for v in REPRESAS.values()})
    filas = supa.select("sige_hidro_represas", {"select": "represa,fecha,afluente_m3s,defluente_m3s",
                                                "represa": "in.(" + ",".join(claves) + ")", "fecha": f"gte.{desde}"})
    out = {k: {} for k in REPRESAS}
    for r in filas:
        for k, (clave, campo, _, _) in REPRESAS.items():
            v = r.get(campo)
            if r["represa"] == clave and v is not None and 1 <= float(v) <= 60000:
                out[k][r["fecha"]] = float(v) / 1000
    return out


def _ana(desde, telemetria_dias):
    from . import ana
    hasta = dt.date.today().isoformat()
    out = {}
    for k, (cod, _, nombre) in ANA.items():
        try:
            s = ana.historica(cod, desde, hasta)
        except Exception as ex:
            print(f"mejoras: {nombre} histórica sin datos ({str(ex)[:80]})")
            s = {}
        if telemetria_dias:
            ini = max(dt.date.fromisoformat(max(s)) + dt.timedelta(days=1) if s else dt.date.fromisoformat(desde),
                      dt.date.today() - dt.timedelta(days=telemetria_dias))
            if ini.isoformat() <= hasta:
                try:
                    s.update(ana.telemetria(cod, ini.isoformat(), hasta))
                except Exception as ex:
                    print(f"mejoras: {nombre} telemetría sin datos ({str(ex)[:80]})")
        out[k] = {f: v / 1000 for f, v in s.items() if v is not None and 0 < v < 60000}
        print(f"mejoras: {nombre} {len(out[k])} días" + (f", último {max(out[k])}" if out[k] else ""))
    return out


def _era5(zonas, desde, hasta):
    """Lluvia diaria ERA5 promedio de los puntos de cada zona: {nombre: {fecha: mm}}. Un pedido por punto y por minuto."""
    from .uruguay import _get_json
    Z = {}
    for nombre, pts in zonas.items():
        acum = {}
        for la, lo in pts:
            q = urllib.parse.urlencode({"latitude": la, "longitude": lo, "start_date": desde, "end_date": hasta,
                                        "daily": "precipitation_sum", "timezone": "America/Sao_Paulo"})
            j = _get_json("https://archive-api.open-meteo.com/v1/archive?" + q)
            for f, v in zip(j["daily"]["time"], j["daily"]["precipitation_sum"]):
                if v is not None and 0 <= v <= 300:
                    acum.setdefault(f, []).append(v)
            time.sleep(62)
        Z[nombre] = {f: sum(v) / len(v) for f, v in acum.items()}
        print(f"mejoras: lluvia ERA5 {nombre} {len(Z[nombre])} días")
    return Z


def _reciente(zonas):
    """Últimos 60 días + pronóstico de 16 días (modelo meteorológico): {nombre: {fecha: mm}}."""
    from .uruguay import _get_json
    pts = [(z, la, lo) for z, P in zonas.items() for la, lo in P]
    q = urllib.parse.urlencode({"latitude": ",".join(str(p[1]) for p in pts), "longitude": ",".join(str(p[2]) for p in pts),
                                "daily": "precipitation_sum", "past_days": 60, "forecast_days": 16,
                                "timezone": "America/Sao_Paulo"}, safe=",")
    j = _get_json("https://api.open-meteo.com/v1/forecast?" + q)
    j = j if isinstance(j, list) else [j]
    Z = {z: {} for z in zonas}
    for (z, _, _), d in zip(pts, j):
        for f, v in zip(d["daily"]["time"], d["daily"]["precipitation_sum"]):
            if v is not None and 0 <= v <= 300:
                Z[z].setdefault(f, []).append(v)
    return {z: {f: sum(v) / len(v) for f, v in d.items()} for z, d in Z.items()}


def lluvia_diaria(desde, reciente=False):
    """{id: {fecha: mm}} con ERA5 guardado en sige_hidro_lluvia (baja y guarda lo que falte). Con `reciente`, completa
    los últimos días y el pronóstico de 16 días con el modelo meteorológico (lo que se usa al pronosticar)."""
    from . import supa
    nombres = {v[0]: k for k, v in ZONAS.items()}
    R = {k: {} for k in ZONAS}
    for r in supa.select("sige_hidro_lluvia", {"select": "zona,fecha,mm", "tipo": "eq.era5", "fecha": f"gte.{desde}",
                                               "zona": "in.(" + ",".join(nombres) + ")"}):
        R[nombres[r["zona"]]][r["fecha"]] = float(r["mm"])
    hasta = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    faltan = {}
    for k, (nombre, _, pts, _) in ZONAS.items():
        s = R[k]
        if not s or min(s) > (dt.date.fromisoformat(desde) + dt.timedelta(days=30)).isoformat():
            faltan[nombre] = (desde, pts)
        elif max(s) < hasta:
            faltan[nombre] = ((dt.date.fromisoformat(max(s)) + dt.timedelta(days=1)).isoformat(), pts)
    for nombre, (d0, pts) in faltan.items():
        nuevo = _era5({nombre: pts}, d0, hasta)[nombre]
        supa.upsert("sige_hidro_lluvia", [{"zona": nombre, "fecha": f, "mm": round(v, 2), "tipo": "era5",
                                           "actualizado": dt.date.today().isoformat()} for f, v in nuevo.items()], "zona,fecha,tipo")
        R[nombres[nombre]].update(nuevo)
    if reciente:
        try:
            rec = _reciente({v[0]: v[2] for v in ZONAS.values()})
            for nombre, d in rec.items():
                s = R[nombres[nombre]]
                ult = max(s) if s else "0000"
                s.update({f: v for f, v in d.items() if f > ult})
        except Exception as ex:
            print("mejoras: sin lluvia reciente/pronosticada:", ex)
    return R


def acumulada(diaria):
    """{fecha: mm} → {fecha: lluvia acumulada / ESCALA}. Un día faltante cuenta como 0 mm si hay datos antes y después."""
    if not diaria:
        return {}
    out, tot = {}, 0.0
    d, fin = dt.date.fromisoformat(min(diaria)), dt.date.fromisoformat(max(diaria))
    while d <= fin:
        f = d.isoformat()
        tot += diaria.get(f, 0.0)
        out[f] = round(tot / ESCALA_LLUVIA, 4)
        d += dt.timedelta(days=1)
    return out


def entradas(desde, telemetria_dias=150, reciente=False):
    """Todas las series nuevas {id: {fecha: valor}} listas para usar como entradas del modelo."""
    S = {}
    try:
        S.update(_represas(desde))
    except Exception as ex:
        print("mejoras: represas sin datos:", ex)
    try:
        S.update(_ana(desde, telemetria_dias))
    except Exception as ex:
        print("mejoras: ANA sin datos:", ex)
    try:
        for k, d in lluvia_diaria(desde, reciente).items():
            S[k] = acumulada(d)
    except Exception as ex:
        print("mejoras: lluvia sin datos:", ex)
    return {k: v for k, v in S.items() if v}
