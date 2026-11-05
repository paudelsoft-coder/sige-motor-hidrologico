"""Motor hidrológico SIGE — corre de fondo y deja en Supabase lo que muestra el portal.

Uso:
  python -m motor ingesta [--dias 40]   descarga alturas del INA → sige_hidro_alturas
  python -m motor calibrar              recalibra pesos con 3 años → sige_hidro_modelo
  python -m motor pronostico            proyección 15 días + cruces de nivel → sige_hidro_pronostico / sige_hidro_cruces
  python -m motor represas [--desde 2000]  caudales diarios de Itaipú y del Iguazú (ONS) → sige_hidro_represas
  python -m motor transito              desfases y reglas de represas (si hay datos en sige_hidro_represas)
  python -m motor todo                  ingesta + pronostico
  python -m motor prueba                calcula con los archivos de pruebas/ sin conectarse a nada
"""
import datetime as dt
import json
import os
import sys
import time

from . import nucleo

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def estaciones():
    return json.load(open(os.path.join(RAIZ, "datos", "estaciones.json"), encoding="utf-8"))["estaciones"]


def ingesta(dias=40):
    from . import ina, supa
    hoy = dt.date.today()
    desde, hasta = (hoy - dt.timedelta(days=dias)).isoformat(), (hoy + dt.timedelta(days=1)).isoformat()
    filas, fallos = [], []
    for e in estaciones():
        try:
            s = ina.observaciones(e["serie_id"], desde, hasta, e.get("marea", False))
            filas += [{"estacion_id": e["id"], "fecha": f, "altura_m": v, "fuente": "INA"} for f, v in s.items()]
        except Exception as ex:
            fallos.append(f"{e['nombre']}: {ex}")
        time.sleep(0.4)
    supa.upsert("sige_hidro_alturas", filas, "estacion_id,fecha")
    supa.insertar("sige_hidro_corridas", [{"tarea": "ingesta", "filas": len(filas), "fallos": fallos}])
    print(f"ingesta: {len(filas)} lecturas, {len(fallos)} fallos")
    for f in fallos:
        print("  -", f)


def _series_supa(dias):
    """Alturas por estación y, como entrada extra (id 1000), la descarga de Itaipú en miles de m³/s."""
    from . import supa
    from .calibrar import ITAIPU_ID
    desde = (dt.date.today() - dt.timedelta(days=dias)).isoformat()
    S = {}
    for r in supa.select("sige_hidro_alturas", {"select": "estacion_id,fecha,altura_m", "fecha": f"gte.{desde}", "order": "fecha"}):
        S.setdefault(r["estacion_id"], {})[r["fecha"]] = r["altura_m"]
    for r in supa.select("sige_hidro_represas", {"select": "fecha,defluente_m3s", "represa": "eq.ITAIPU", "fecha": f"gte.{desde}"}):
        if r["defluente_m3s"] is not None:
            S.setdefault(ITAIPU_ID, {})[r["fecha"]] = r["defluente_m3s"] / 1000
    return S


def represas(desde_anio=None):
    """Descarga los caudales del ONS (año actual, o desde `desde_anio`) → sige_hidro_represas."""
    from . import ons, supa
    hoy = dt.date.today()
    anios = range(desde_anio or (hoy.year if hoy.month > 1 else hoy.year - 1), hoy.year + 1)
    total = 0
    for a in anios:
        filas = ons.anio(a)
        supa.upsert("sige_hidro_represas", filas, "represa,fecha")
        total += len(filas)
        print(f"represas {a}: {len(filas)} filas")
    supa.insertar("sige_hidro_corridas", [{"tarea": "represas", "filas": total, "fallos": []}])


def calibrar():
    from . import supa
    from .calibrar import calibrar as cal, SUBE_7D, ITAIPU_ID
    modelo = cal(estaciones(), _series_supa(365 * 3 + 10), dt.date.today(), extras={ITAIPU_ID: "Itaipú (descarga)"})
    hoy = dt.date.today().isoformat()
    supa.upsert("sige_hidro_modelo", [{"estacion_id": int(k), "calibrado": hoy, "sube_7d": SUBE_7D, **v} for k, v in modelo.items()], "estacion_id")
    print("calibrar:", len(modelo), "puertos con modelo")


COLUMNAS_CRUCE = ("nivel", "tipo", "umbral", "momento", "desde_rango", "hasta_rango", "sale", "en_nivel_desde",
                  "desde_antes_de_serie", "permanencia_h", "permanencia_minima")


def _calcular(S, modelo, sube_7d):
    emitido = dt.datetime.now(nucleo.ART).isoformat(timespec="minutes")
    pron, cru = [], []
    for e in estaciones():
        proy = nucleo.proyectar(S, modelo, e["id"], sube_7d)
        if not proy:
            continue
        pron += [{"estacion_id": e["id"], "emitido": emitido, "dato_fecha": max(S[e["id"]]), **q} for q in proy]
        for ev in nucleo.cruces(S[e["id"]], proy, e["alerta"], e["evac"]):
            # PostgREST exige las mismas columnas en todas las filas de un lote
            fila = {"estacion_id": e["id"], "emitido": emitido, **{k: None for k in COLUMNAS_CRUCE}}
            fila.update({k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in ev.items()})
            cru.append(fila)
    return pron, cru


def pronostico():
    from . import supa
    S = _series_supa(40)
    filas = supa.select("sige_hidro_modelo", {"select": "*"})
    modelo = {str(r["estacion_id"]): r for r in filas}
    sube = filas[0]["sube_7d"] if filas else 0.15
    pron, cru = _calcular(S, modelo, sube)
    supa.borrar("sige_hidro_pronostico", {"estacion_id": "gte.0"})
    supa.insertar("sige_hidro_pronostico", pron)
    supa.borrar("sige_hidro_cruces", {"estacion_id": "gte.0"})
    supa.insertar("sige_hidro_cruces", cru)
    supa.insertar("sige_hidro_corridas", [{"tarea": "pronostico", "filas": len(pron), "fallos": []}])
    print(f"pronostico: {len(pron)} filas, {len(cru)} cruces")


def transito():
    """Calibra desfases y la regla del vertedero cuando ya hay historia de represas cargada."""
    from . import supa
    from . import transito as T
    import numpy as np
    rep = supa.select("sige_hidro_represas", {"select": "*", "order": "fecha"})
    if len(rep) < 120:
        print(f"transito: hay {len(rep)} días de represas; hacen falta al menos 120 para calibrar")
        return
    fechas = sorted({r["fecha"] for r in rep})
    idx = {f: i for i, f in enumerate(fechas)}
    serie = lambda nombre, campo: _col(rep, idx, nombre, campo, len(fechas))
    S = _series_supa(len(fechas) + 5)
    out = []
    for e in estaciones():
        h = np.array([S.get(e["id"], {}).get(f, np.nan) for f in fechas], dtype=float)
        for represa in ("ITAIPU", "YACYRETA"):
            r = T.desfase(serie(represa, "defluente_m3s"), h)
            if r:
                out.append({"origen": represa, "estacion_id": e["id"], **r})
    yac = T.regla_vertedero(serie("YACYRETA", "defluente_m3s"), serie("YACYRETA", "vertido_m3s"))
    supa.upsert("sige_hidro_transito", out, "origen,estacion_id")
    print("transito:", len(out), "tramos calibrados; regla Yacyretá:", yac)


def _col(rep, idx, nombre, campo, n):
    import numpy as np
    a = np.full(n, np.nan)
    for r in rep:
        if r["represa"] == nombre and r.get(campo) is not None:
            a[idx[r["fecha"]]] = r[campo]
    return a


def prueba():
    a = json.load(open(os.path.join(RAIZ, "pruebas", "alturas_vigia.json"), encoding="utf-8"))
    m = json.load(open(os.path.join(RAIZ, "pruebas", "modelo_vigia.json"), encoding="utf-8"))
    S = {s["id"]: dict((f, v) for f, v in s["serie"]) for s in a["estaciones"]}
    pron, cru = _calcular(S, m["estaciones"], m["sube_7d"])
    nom = {e["id"]: e["nombre"] for e in estaciones()}
    print(f"{len(pron)} filas de pronóstico, {len(cru)} cruces")
    for c in sorted(cru, key=lambda x: x["momento"] or "9"):
        if c["tipo"] != "POSIBLE":
            print(f"  {nom[c['estacion_id']]:28s} {c['tipo']:6s} {c['nivel']:10s} {c['momento'] or 'más de 15 días'}")


if __name__ == "__main__":
    tarea = sys.argv[1] if len(sys.argv) > 1 else "todo"
    if tarea == "ingesta":
        ingesta(int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--dias" else 40)
    elif tarea == "calibrar":
        calibrar()
    elif tarea == "pronostico":
        pronostico()
    elif tarea == "transito":
        transito()
    elif tarea == "represas":
        represas(int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--desde" else None)
    elif tarea == "prueba":
        prueba()
    else:
        ingesta()
        try:
            represas()
        except Exception as ex:   # si el ONS no responde, el pronóstico sigue con las alturas
            print("represas: sin actualizar:", ex)
        pronostico()
