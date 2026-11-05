"""Motor hidrológico SIGE — corre de fondo y deja en Supabase lo que muestra el portal.

Uso:
  python -m motor ingesta [--dias 40]   descarga alturas del INA → sige_hidro_alturas
  python -m motor calibrar              recalibra pesos con 3 años → sige_hidro_modelo
  python -m motor pronostico            proyección 15 días + cruces de nivel → sige_hidro_pronostico / sige_hidro_cruces
  python -m motor represas [--desde 2000]  caudales diarios de Itaipú y del Iguazú (ONS) → sige_hidro_represas
  python -m motor transito              desfases y reglas de represas (si hay datos en sige_hidro_represas)
  python -m motor todo                  ingesta + represas + pronostico
  python -m motor rapida                solo si el INA publicó alturas nuevas: las guarda y recalcula el pronóstico
  python -m motor historia              baja la serie histórica completa del INA (desde 1900) → sige_hidro_historia
  python -m motor analogos              situaciones parecidas en la historia para cada puerto → sige_hidro_analogos
  python -m motor evaluar               compara calibrar con 3, 10 y 30 años sobre los últimos 2 años (no guarda nada)
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


def rapida(dias=3):
    """Revisión liviana (cada 10 minutos por la mañana): baja los últimos días del INA y solo guarda y recalcula
    si apareció un dato nuevo o corregido. Así el portal no se recarga cuando no cambió nada."""
    from . import ina, supa
    hoy = dt.date.today()
    desde, hasta = (hoy - dt.timedelta(days=dias)).isoformat(), (hoy + dt.timedelta(days=1)).isoformat()
    antes = {(r["estacion_id"], r["fecha"]): round(float(r["altura_m"]), 3)
             for r in supa.select("sige_hidro_alturas", {"select": "estacion_id,fecha,altura_m", "fecha": f"gte.{desde}"})}
    nuevas, fallos = [], []
    for e in estaciones():
        try:
            for f, v in ina.observaciones(e["serie_id"], desde, hasta, e.get("marea", False)).items():
                if antes.get((e["id"], f)) != round(float(v), 3):
                    nuevas.append({"estacion_id": e["id"], "fecha": f, "altura_m": v, "fuente": "INA"})
        except Exception as ex:
            fallos.append(f"{e['nombre']}: {ex}")
        time.sleep(0.2)
    if not nuevas:
        print(f"rapida: sin datos nuevos ({len(fallos)} fallos)")
        return False
    supa.upsert("sige_hidro_alturas", nuevas, "estacion_id,fecha")
    supa.insertar("sige_hidro_corridas", [{"tarea": "rapida", "filas": len(nuevas), "fallos": fallos}])
    print(f"rapida: {len(nuevas)} lecturas nuevas o corregidas")
    return True


DESCARTADAS = {}   # {estacion_id: {fecha: altura}} lecturas que el control de calidad dejó afuera en esta corrida


def _series_supa(dias):
    """Alturas por estación (depuradas) y, como entrada extra (id 1000), la descarga de Itaipú en miles de m³/s.
    Las alturas quedan guardadas tal cual las publica el INA; la depuración se hace al leerlas."""
    from . import supa
    from .calibrar import ITAIPU_ID
    desde = (dt.date.today() - dt.timedelta(days=max(dias, 400))).isoformat()   # ≥ 400 días para conocer el salto creíble
    corte = (dt.date.today() - dt.timedelta(days=dias)).isoformat()
    S = {}
    for r in supa.select("sige_hidro_alturas", {"select": "estacion_id,fecha,altura_m", "fecha": f"gte.{desde}", "order": "fecha"}):
        S.setdefault(r["estacion_id"], {})[r["fecha"]] = float(r["altura_m"])
    DESCARTADAS.clear()
    for k in list(S):
        limpia, _ = nucleo.depurar(S[k])
        limpia, malas = nucleo.depurar(S[k], nucleo.salto_tipico(limpia))
        S[k] = {f: v for f, v in limpia.items() if f >= corte}
        if malas:
            DESCARTADAS[k] = malas
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
        try:
            filas = ons.anio(a)
        except Exception as ex:   # el ONS no tiene todos los años publicados
            print(f"represas {a}: sin datos ({ex})")
            continue
        supa.upsert("sige_hidro_represas", filas, "represa,fecha")
        total += len(filas)
        print(f"represas {a}: {len(filas)} filas")
    supa.insertar("sige_hidro_corridas", [{"tarea": "represas", "filas": total, "fallos": []}])


ANIOS_CAL = 3   # años de historia para calibrar (se decide con `python -m motor evaluar`)


def historia():
    """Serie diaria completa del INA para cada puerto, guardada compacta: una fila por puerto y año."""
    from . import ina, supa
    hasta = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    for e in estaciones():
        try:
            s = ina.observaciones(e["serie_id"], "1900-01-01", hasta, e.get("marea", False))
        except Exception as ex:
            print(f"historia {e['nombre']}: sin datos ({ex})")
            continue
        anios = {}
        for f, v in s.items():
            d = dt.date.fromisoformat(f)
            anios.setdefault(d.year, [None] * 366)[d.timetuple().tm_yday - 1] = round(v, 3)
        filas = [{"estacion_id": e["id"], "anio": a, "valores": vals} for a, vals in sorted(anios.items())]
        supa.upsert("sige_hidro_historia", filas, "estacion_id,anio", lote=40)
        print(f"historia {e['nombre']}: {len(s)} días desde {min(s)[:4] if s else '-'}")
        time.sleep(1)


def _historia_supa(desde_anio=1900):
    """{estacion_id: {fecha: altura}} desde la tabla histórica, más las lecturas recientes de sige_hidro_alturas."""
    from . import supa
    S = {}
    for r in supa.select("sige_hidro_historia", {"select": "estacion_id,anio,valores", "anio": f"gte.{desde_anio}"}, pagina=200):
        d0 = dt.date(r["anio"], 1, 1)
        dest = S.setdefault(r["estacion_id"], {})
        for i, v in enumerate(r["valores"]):
            if v is not None:
                dest[(d0 + dt.timedelta(days=i)).isoformat()] = float(v)
    desde = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    for r in supa.select("sige_hidro_alturas", {"select": "estacion_id,fecha,altura_m", "fecha": f"gte.{desde}"}):
        S.setdefault(r["estacion_id"], {})[r["fecha"]] = float(r["altura_m"])
    return S


def _series_largas(anios):
    """Historia depurada de `anios` años + Itaipú, para calibrar."""
    from . import supa
    from .calibrar import ITAIPU_ID
    S = _historia_supa(dt.date.today().year - anios - 1)
    if not S:   # sin historia cargada: lo de siempre
        return _series_supa(365 * anios + 10)
    for k in list(S):
        limpia, _ = nucleo.depurar(S[k])
        S[k], _ = nucleo.depurar(S[k], nucleo.salto_tipico(limpia))
    desde = (dt.date.today() - dt.timedelta(days=365 * anios + 10)).isoformat()
    for r in supa.select("sige_hidro_represas", {"select": "fecha,defluente_m3s", "represa": "eq.ITAIPU", "fecha": f"gte.{desde}"}):
        if r["defluente_m3s"] is not None:
            S.setdefault(ITAIPU_ID, {})[r["fecha"]] = r["defluente_m3s"] / 1000
    return S


def evaluar():
    """Calibra con 3, 10 y 30 años usando datos hasta hace 2 años y mide el error en los 2 años siguientes."""
    from .calibrar import calibrar as cal, ITAIPU_ID
    hoy = dt.date.today()
    corte = hoy - dt.timedelta(days=730)
    S = _series_largas(32)
    res = {}
    for anios in (3, 10, 30):
        Sc = {k: {f: v for f, v in s.items() if f <= corte.isoformat()} for k, s in S.items()}
        modelo = cal(estaciones(), Sc, corte, anios=anios, log=lambda *a: None, extras={ITAIPU_ID: "Itaipú (descarga)"})
        res[anios] = _error_fuera(S, modelo, corte, hoy)
        print(f"evaluar: {anios} años → {len(modelo)} modelos")
    print(f"{'puerto':26s} " + " ".join(f"{a:>2d}a·3d {a:>2d}a·7d" for a in (3, 10, 30)) + "  persist·7d")
    total = {a: [] for a in (3, 10, 30)}
    for e in estaciones():
        k = e["id"]
        fila = []
        for a in (3, 10, 30):
            r = res[a].get(k)
            fila.append(f"{r['e3']:7.2f} {r['e7']:7.2f}" if r else "      -       -")
            if r:
                total[a].append(r["e7"])
        per = next((res[a][k]["p7"] for a in (3, 10, 30) if res[a].get(k)), None)
        print(f"{e['nombre'][:26]:26s} " + " ".join(fila) + (f"  {per:9.2f}" if per else ""))
    for a in (3, 10, 30):
        v = total[a]
        print(f"promedio error 7 días con {a} años: {sum(v) / len(v):.3f} m en {len(v)} puertos" if v else f"{a} años: sin modelos")


def _error_fuera(S, modelo, desde, hasta):
    """Error (RMSE) a 3 y 7 días de cada modelo en el período (desde, hasta], uno de cada 2 días."""
    out = {}
    for k, M in modelo.items():
        s = S.get(int(k))
        if not s:
            continue
        e3, e7, p7 = [], [], []
        f = desde + dt.timedelta(days=1)
        while f <= hasta - dt.timedelta(days=7):
            t = f.isoformat()
            f += dt.timedelta(days=2)
            if t not in s:
                continue
            x = [nucleo.cambio_entrada(S.get(int(en["id"]), {}), t, en["desde"], en["hasta"]) for en in M["entradas"]]
            if any(v is None for v in x):
                continue
            for kk, lista in ((3, e3), (7, e7)):
                obs = s.get(nucleo.sumar_dias(t, kk))
                if obs is None:
                    continue
                c = s[t] + sum(w * v for w, v in zip(M["w"][kk - 1], x))
                lista.append(c - obs)
                if kk == 7:
                    p7.append(s[t] - obs)
        if e7 and e3:
            rm = lambda v: (sum(z * z for z in v) / len(v)) ** 0.5
            out[int(k)] = {"e3": rm(e3), "e7": rm(e7), "p7": rm(p7)}
    return out


def analogos():
    """Situaciones parecidas en la historia (toda la serie disponible) para cada puerto → sige_hidro_analogos."""
    from . import supa
    S = _historia_supa(1900)
    ahora = dt.datetime.now(nucleo.ART).isoformat(timespec="minutes")
    filas = []
    for e in estaciones():
        s = S.get(e["id"])
        if not s:
            continue
        limpia, _ = nucleo.depurar(s)
        limpia, _ = nucleo.depurar(s, nucleo.salto_tipico(limpia))
        r = nucleo.analogos(limpia, e["alerta"], e["evac"])
        if not r:
            continue
        sit, res, casos = r
        filas.append({"estacion_id": e["id"], "calculado": ahora, "situacion": sit, "resumen": res, "casos": casos})
        print(f"analogos {e['nombre']}: {res.get('n')} casos desde {res.get('desde')} · hoy más alto que el {res.get('rareza_pct')} % de esa época")
    if filas:
        supa.upsert("sige_hidro_analogos", filas, "estacion_id")


def calibrar():
    from . import supa
    from .calibrar import calibrar as cal, SUBE_7D, ITAIPU_ID
    S = _series_largas(ANIOS_CAL) if ANIOS_CAL > 3 else _series_supa(365 * 3 + 10)
    modelo = cal(estaciones(), S, dt.date.today(), anios=ANIOS_CAL, extras={ITAIPU_ID: "Itaipú (descarga)"})
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
    recientes = [f"{k} {f}={v}" for k, m in DESCARTADAS.items() for f, v in m.items()
                 if f >= (dt.date.today() - dt.timedelta(days=10)).isoformat()]
    supa.insertar("sige_hidro_corridas", [{"tarea": "pronostico", "filas": len(pron),
                                           "fallos": [f"dato descartado por control de calidad: {x}" for x in recientes]}])
    print(f"pronostico: {len(pron)} filas, {len(cru)} cruces")
    for x in recientes:
        print("  - dato descartado por control de calidad:", x)


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
    elif tarea == "rapida":
        if rapida():
            pronostico()
    elif tarea == "historia":
        historia()
        represas(2000)
    elif tarea == "analogos":
        analogos()
    elif tarea == "evaluar":
        evaluar()
    else:
        ingesta()
        try:
            represas()
        except Exception as ex:   # si el ONS no responde, el pronóstico sigue con las alturas
            print("represas: sin actualizar:", ex)
        pronostico()
        try:
            analogos()
        except Exception as ex:   # si todavía no se cargó la historia, el resto sigue igual
            print("analogos: sin actualizar:", ex)
