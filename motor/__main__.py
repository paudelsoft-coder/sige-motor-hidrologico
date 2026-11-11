"""Motor hidrológico SIGE — corre de fondo y deja en Supabase lo que muestra el portal.

Uso:
  python -m motor ingesta [--dias 40]   descarga alturas del INA → sige_hidro_alturas
  python -m motor calibrar              recalibra pesos con 3 años → sige_hidro_modelo
  python -m motor pronostico            proyección 15 días + cruces de nivel → sige_hidro_pronostico / sige_hidro_cruces
  python -m motor represas [--desde 2000]  caudales diarios de Itaipú y del Iguazú (ONS) y de Salto Grande (INA) → sige_hidro_represas
  python -m motor transito              desfases y reglas de represas (si hay datos en sige_hidro_represas)
  python -m motor todo                  ingesta + represas + pronostico
  python -m motor rapida                solo si el INA publicó alturas nuevas: las guarda y recalcula el pronóstico
  python -m motor historia              baja la serie histórica completa del INA (desde 1900) → sige_hidro_historia
  python -m motor analogos              situaciones parecidas en la historia para cada puerto → sige_hidro_analogos
  python -m motor evaluar               mide si GloFAS (caudal simulado, Open-Meteo) mejora el pronóstico (no guarda nada)
  python -m motor calibrar_glofas       modelo de prueba con GloFAS → sige_hidro_modelo_glofas (también corre con calibrar)
  python -m motor glofas                baja GloFAS y guarda el pronóstico de prueba del día (también corre con todo)
  python -m motor evaluar_prueba        compara el pronóstico de prueba con lo que realmente pasó
  python -m motor calibrar_uruguay      modelo del río Uruguay (Salto Grande → Concordia) → sige_hidro_modelo_uruguay
  python -m motor evaluar_uruguay       prueba del modelo del Uruguay con todos los datos desde 2021 (no guarda nada)
  python -m motor uruguay               pronóstico de prueba del río Uruguay → sige_hidro_pron_uruguay (también corre con todo)
  python -m motor auditar               revisa la historia de cada puerto (vacíos, imposibles, picos) sin cambiar nada
  python -m motor auditar_borrar        lo mismo y borra las filas sin ningún valor
  python -m motor evaluar_cruces        prueba hacia atrás de los avisos de alerta / evacuación (2008-2023)
  python -m motor mejorar               compara variantes del modelo (Brasil, Salto Grande, lluvia) con prueba hacia atrás
  python -m motor cuencas               lluvia caída / pronosticada, suelo y aporte de cada cuenca (Open-Meteo) → sige_hidro_cuencas
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


def _prefectura(fallos):
    """Lecturas de la tabla de Prefectura (más nuevas que las del INA). Guarda la última de cada puerto en
    sige_hidro_ultima (la "altura actual" del portal) y devuelve todas para completar la serie diaria (00 h)."""
    from . import prefectura, supa
    try:
        lec = prefectura.lecturas(estaciones())
    except Exception as ex:
        fallos.append(f"Prefectura: {ex}")
        return []
    ult = {}
    for l in lec:
        if l["estacion_id"] not in ult or (l["fecha"], l["hora"] or "") > (ult[l["estacion_id"]]["fecha"], ult[l["estacion_id"]]["hora"] or ""):
            ult[l["estacion_id"]] = l
    try:
        ahora = dt.datetime.now(dt.timezone.utc).isoformat()
        supa.upsert("sige_hidro_ultima", [{**l, "fuente": "PNA", "actualizado": ahora} for l in ult.values()], "estacion_id")
    except Exception as ex:
        print("prefectura: no se pudo guardar la última lectura (¿falta la tabla sige_hidro_ultima?):", str(ex)[:120])
    print(f"prefectura: {len(lec)} lecturas de {len(ult)} puertos")
    return lec


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
    nuevas, fallos, vistos = [], [], set()
    for e in estaciones():
        try:
            for f, v in ina.observaciones(e["serie_id"], desde, hasta, e.get("marea", False)).items():
                vistos.add((e["id"], f))
                if antes.get((e["id"], f)) != round(float(v), 3):
                    nuevas.append({"estacion_id": e["id"], "fecha": f, "altura_m": v, "fuente": "INA"})
        except Exception as ex:
            fallos.append(f"{e['nombre']}: {ex}")
        time.sleep(0.2)

    if not nuevas:
        # Las lecturas de Prefectura las carga la PC de la sala (sige_prefectura.py) directo en la base: si hay un
        # dato más nuevo que el último pronóstico de algún puerto, se recalcula aunque el INA no haya publicado nada.
        try:
            dato = {r["estacion_id"]: r["dato_fecha"] for r in supa.select("sige_hidro_pronostico", {"select": "estacion_id,dato_fecha", "k": "eq.1"})}
            ult = {}
            for (eid, f) in antes:
                ult[eid] = max(ult.get(eid, f), f)
            ayer = (hoy - dt.timedelta(days=1)).isoformat()
            nuevos = [eid for eid, f in ult.items() if dato.get(eid) and f > dato[eid] and f >= ayer]
        except Exception as ex:
            nuevos = []
            print("rapida: no se pudo comparar con el último pronóstico:", ex)
        if nuevos:
            print(f"rapida: {len(nuevos)} puertos con lectura de Prefectura más nueva que el pronóstico → se recalcula")
            supa.insertar("sige_hidro_corridas", [{"tarea": "rapida", "filas": 0, "fallos": fallos + [f"PNA nuevos: {len(nuevos)}"]}])
            return True
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
        S[k] = limpia
        if malas:
            DESCARTADAS[k] = malas
    nucleo.restituir(S, DESCARTADAS, estaciones())   # crecidas rápidas confirmadas por un puerto vecino
    for k in list(S):
        S[k] = {f: v for f, v in S[k].items() if f >= corte}
        if not DESCARTADAS.get(k):
            DESCARTADAS.pop(k, None)
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
    try:
        total += salto_grande(f"{desde_anio}-01-01" if desde_anio else (hoy - dt.timedelta(days=60)).isoformat())
    except Exception as ex:   # si el INA no responde, Itaipú y el resto siguen igual
        print("represas Salto Grande: sin datos", ex)
    supa.insertar("sige_hidro_corridas", [{"tarea": "represas", "filas": total, "fallos": []}])


# Salto Grande (CTM) según el INA: caudal medio diario que entra al embalse, caudal medio diario aguas abajo
# (lo que eroga la represa) y altura media diaria del embalse.
SALTO_GRANDE = {"afluente_m3s": 26674, "defluente_m3s": 26675, "cota_m": 26319}


def salto_grande(desde):
    from . import ina, supa
    hasta = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    datos = {}
    for campo, serie in SALTO_GRANDE.items():
        for f, v in ina.observaciones(serie, desde, hasta).items():
            if v and v > 0:   # el INA guarda ceros cuando falta el dato
                datos.setdefault(f, {})[campo] = round(float(v), 2)
        time.sleep(1)
    filas = [{"represa": "SALTO_GRANDE", "fecha": f, "afluente_m3s": d.get("afluente_m3s"), "turbinado_m3s": None,
              "vertido_m3s": None, "defluente_m3s": d.get("defluente_m3s"), "cota_m": d.get("cota_m"),
              "fuente": "INA (CTM Salto Grande)"} for f, d in sorted(datos.items())]
    supa.upsert("sige_hidro_represas", filas, "represa,fecha")
    print(f"represas Salto Grande: {len(filas)} días desde {desde}")
    return len(filas)


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
    malas = {}
    for k in list(S):
        limpia, _ = nucleo.depurar(S[k])
        S[k], malas[k] = nucleo.depurar(S[k], nucleo.salto_tipico(limpia))
    nucleo.restituir(S, malas, estaciones())   # crecidas rápidas confirmadas por un puerto vecino
    desde = (dt.date.today() - dt.timedelta(days=365 * anios + 10)).isoformat()
    for r in supa.select("sige_hidro_represas", {"select": "fecha,defluente_m3s", "represa": "eq.ITAIPU", "fecha": f"gte.{desde}"}):
        if r["defluente_m3s"] is not None:
            S.setdefault(ITAIPU_ID, {})[r["fecha"]] = r["defluente_m3s"] / 1000
    return S


def evaluar():
    """¿Mejora el pronóstico con GloFAS? Calibra con los 3 años previos a hace 2 años y mide el error en los
    2 años siguientes, en tres variantes:
      base    → alturas del INA + Itaipú (lo que corre hoy)
      +GloFAS → además el caudal GloFAS de los últimos días en puntos aguas arriba (dato que ya existe al pronosticar)
      +pron.  → además el cambio de caudal que GloFAS anticipa para los próximos 3/7/14 días. En esta prueba se usa
                lo que GloFAS simuló con la lluvia que realmente cayó, así que es un techo: en la operación real el
                pronóstico de lluvia tiene error y la mejora va a ser menor."""
    from . import externas
    from .calibrar import calibrar as cal, ITAIPU_ID
    hoy = dt.date.today()
    corte = hoy - dt.timedelta(days=730)
    S = _series_largas(6)
    G = externas.caudales((corte - dt.timedelta(days=365 * 3 + 30)).isoformat(), hoy.isoformat())
    print("evaluar: GloFAS " + ", ".join(f"{externas.PUNTOS[k][0]} {len(v)} días" for k, v in G.items()))
    S.update(G)
    variantes = {
        "base": {ITAIPU_ID: "Itaipú (descarga)"},
        "+GloFAS": {ITAIPU_ID: "Itaipú (descarga)", **{k: {**v, "futuro": False} for k, v in externas.extras().items()}},
        "+pron.": {ITAIPU_ID: "Itaipú (descarga)", **externas.extras()},
    }
    res, mods = {}, {}
    Sc = {k: {f: v for f, v in s.items() if f <= corte.isoformat()} for k, s in S.items()}
    for nom, ex in variantes.items():
        # para calibrar, la serie GloFAS llega hasta el corte (las ventanas hacia adelante usan días ya pasados)
        modelo = cal(estaciones(), Sc, corte, anios=3, log=lambda *a: None, extras=ex)
        res[nom], mods[nom] = _error_fuera(S, modelo, corte, hoy), modelo
        print(f"evaluar: {nom} → {len(modelo)} modelos")
    V = list(variantes)
    print(f"{'puerto':26s} " + " ".join(f"{v:>8s}·3d {v:>8s}·7d" for v in V) + "  persist·7d  entradas (+pron.)")
    total = {v: [] for v in V}
    for e in estaciones():
        k = e["id"]
        if not all(res[v].get(k) for v in V):
            continue
        fila = [f"{res[v][k]['e3']:11.2f} {res[v][k]['e7']:11.2f}" for v in V]
        for v in V:
            total[v].append(res[v][k]["e7"])
        ent = ", ".join(f"{x['nombre'][:22]} {x['desde']}/{x['hasta']}" for x in mods["+pron."][str(k)]["entradas"])
        print(f"{e['nombre'][:26]:26s} " + " ".join(fila) + f"  {res['base'][k]['p7']:9.2f}  {ent}")
    for v in V:
        t = total[v]
        print(f"promedio error 7 días {v:8s}: {sum(t) / len(t):.3f} m en {len(t)} puertos")


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
        sin = float("inf")   # puertos sin niveles oficiales: solo la parte de alturas, sin cruces de nivel
        r = nucleo.analogos(limpia, e["alerta"] if e["alerta"] is not None else sin, e["evac"] if e["evac"] is not None else sin)
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


# ---------------------------------------------------------------- GloFAS en modo prueba (el portal no lo usa)

def _glofas_supa(desde):
    from . import supa
    G = {}
    for r in supa.select("sige_hidro_glofas", {"select": "punto_id,fecha,caudal_milm3s", "fecha": f"gte.{desde}"}):
        G.setdefault(r["punto_id"], {})[r["fecha"]] = float(r["caudal_milm3s"])
    return G


def calibrar_glofas():
    """Igual que calibrar(), pero con el caudal GloFAS como entrada posible → sige_hidro_modelo_glofas."""
    from . import externas, supa
    from .calibrar import calibrar as cal, SUBE_7D, ITAIPU_ID
    hoy = dt.date.today()
    S = _series_supa(365 * 3 + 10)
    S.update(externas.caudales((hoy - dt.timedelta(days=365 * 3 + 40)).isoformat(), hoy.isoformat()))
    modelo = cal(estaciones(), S, hoy, anios=3, log=lambda *a: None,
                 extras={ITAIPU_ID: "Itaipú (descarga)", **externas.extras()})
    supa.upsert("sige_hidro_modelo_glofas", [{"estacion_id": int(k), "calibrado": hoy.isoformat(), "sube_7d": SUBE_7D, **v}
                                             for k, v in modelo.items()], "estacion_id")
    usan = [k for k, m in modelo.items() if any(x["id"] >= 1100 for x in m["entradas"])]
    print(f"calibrar_glofas: {len(modelo)} puertos con modelo, {len(usan)} usan GloFAS")


def glofas_prueba():
    """Baja GloFAS (45 días atrás + 30 de pronóstico) y guarda el pronóstico de prueba del día junto al normal."""
    from . import externas, supa
    hoy = dt.date.today()
    G = externas.caudales((hoy - dt.timedelta(days=45)).isoformat(), (hoy + dt.timedelta(days=29)).isoformat())
    supa.upsert("sige_hidro_glofas", [{"punto_id": k, "fecha": f, "caudal_milm3s": v, "descargado": hoy.isoformat()}
                                      for k, s in G.items() for f, v in s.items()], "punto_id,fecha")
    mg = {str(r["estacion_id"]): r for r in supa.select("sige_hidro_modelo_glofas", {"select": "*"})}
    if not mg:
        print("glofas: caudales guardados; falta el modelo de prueba (python -m motor calibrar_glofas)")
        return
    mb = {str(r["estacion_id"]): r for r in supa.select("sige_hidro_modelo", {"select": "*"})}
    S = _series_supa(40)
    S.update(G)
    filas = []
    for e in estaciones():
        pg = nucleo.proyectar(S, mg, e["id"])
        if not pg:
            continue
        base = {q["k"]: q["central"] for q in (nucleo.proyectar(S, mb, e["id"]) or [])}
        filas += [{"estacion_id": e["id"], "emitido": hoy.isoformat(), "dato_fecha": max(S[e["id"]]), "k": q["k"],
                   "fecha": q["fecha"], "central": q["central"], "base_central": base.get(q["k"])} for q in pg]
    supa.upsert("sige_hidro_pron_prueba", filas, "estacion_id,emitido,k")
    print(f"glofas: {sum(len(v) for v in G.values())} caudales, pronóstico de prueba para {len(filas) // 15} puertos")


def evaluar_prueba():
    """Compara el pronóstico de prueba (con GloFAS) y el normal contra la altura que realmente se midió."""
    from . import supa
    P = supa.select("sige_hidro_pron_prueba", {"select": "estacion_id,emitido,dato_fecha,k,fecha,central,base_central"})
    S = _series_supa(120)
    acc = {}
    for r in P:
        obs = S.get(r["estacion_id"], {}).get(r["fecha"])
        if r["k"] not in (3, 7) or obs is None or r["central"] is None or r["base_central"] is None:
            continue
        a = acc.setdefault(r["estacion_id"], {3: ([], []), 7: ([], [])})[r["k"]]
        a[0].append(float(r["base_central"]) - obs)
        a[1].append(float(r["central"]) - obs)
    rm = lambda v: (sum(z * z for z in v) / len(v)) ** 0.5 if v else float("nan")
    emis = sorted({r["emitido"] for r in P})
    print(f"evaluar_prueba: {len(emis)} días emitidos ({emis[0] if emis else '-'} a {emis[-1] if emis else '-'})")
    print(f"{'puerto':26s}  n·7d  normal·3d  GloFAS·3d  normal·7d  GloFAS·7d")
    tot = [[], []]
    for e in estaciones():
        a = acc.get(e["id"])
        if not a or not a[7][0]:
            continue
        tot[0] += a[7][0]; tot[1] += a[7][1]
        print(f"{e['nombre'][:26]:26s} {len(a[7][0]):5d} {rm(a[3][0]):10.2f} {rm(a[3][1]):10.2f} {rm(a[7][0]):10.2f} {rm(a[7][1]):10.2f}")
    if tot[0]:
        print(f"todos los puertos, error a 7 días: normal {rm(tot[0]):.3f} m · con GloFAS {rm(tot[1]):.3f} m")


# ---------------------------------------------------------------- río Uruguay (Salto Grande → Concordia), modo prueba

def _lluvia_supa(desde):
    from . import supa
    R = {}
    for r in supa.select("sige_hidro_lluvia", {"select": "zona,fecha,mm", "tipo": "eq.era5", "fecha": f"gte.{desde}"}):
        R.setdefault(r["zona"], {})[r["fecha"]] = float(r["mm"])
    return R


def _lluvia_historica(desde):
    """Lluvia ERA5 guardada; si falta un tramo, la baja de Open-Meteo y la guarda (la primera vez tarda ~10 min)."""
    from . import supa, uruguay as U
    R = _lluvia_supa(desde)
    ultima = min((max(R[z]) for z in U.ZONAS if R.get(z)), default=None)
    hasta = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    if ultima is None or ultima < hasta:
        nuevo = U.lluvia_historica(desde if ultima is None else U.sumar_dias(ultima, 1), hasta)
        supa.upsert("sige_hidro_lluvia", [{"zona": z, "fecha": f, "mm": round(v, 2), "tipo": "era5", "actualizado": dt.date.today().isoformat()}
                                          for z, d in nuevo.items() for f, v in d.items()], "zona,fecha,tipo")
        for z, d in nuevo.items():
            R.setdefault(z, {}).update(d)
        print(f"uruguay: lluvia ERA5 agregada {sum(len(d) for d in nuevo.values())} datos")
    return R


def _datos_uruguay(desde, telemetria_dias=0):
    from . import ana, uruguay as U
    D, informe = U.series_ina(desde)
    print("uruguay: limpieza " + ", ".join(f"{k} -{v['imposibles'] + v['picos'] + v['congelados']}" for k, v in informe.items()))
    try:   # escalas de Brasil (ANA): si no responde, el modelo sin ellas sigue funcionando
        for k, v in ana.series(desde, max_telemetria=telemetria_dias).items():
            D[k], inf = U.limpiar(v, 1, 60000, 15)
            print(f"uruguay: {k} limpieza -{inf['imposibles'] + inf['picos'] + inf['congelados']}")
    except Exception as ex:
        print("uruguay: escalas de Brasil sin datos:", ex)
    B = U.represas_br(desde)
    if not B.get("FOZ_CHAPECO"):
        print("uruguay: faltan las represas de Brasil, las bajo del ONS")
        represas(2010)
        B = U.represas_br(desde)
    return D, B


def calibrar_uruguay():
    from . import supa, uruguay as U
    D, B = _datos_uruguay("2011-01-01")
    R = _lluvia_historica("2011-01-01")
    M = U.calibrar_modelos(D, R, B, dt.date.today().isoformat())
    M0 = U.calibrar_modelos(D, R, B, dt.date.today().isoformat(), mej=U.MEJORAS_SIN_ANA)
    supa.upsert("sige_hidro_modelo_uruguay", [{"id": "modelo", "datos": M, "calibrado": dt.date.today().isoformat()},
                                              {"id": "modelo_sin_ana", "datos": M0, "calibrado": dt.date.today().isoformat()}], "id")
    print(f"calibrar_uruguay: modelos para {U.HORIZONTES} días · alfa {M['alfa']}")


def evaluar_uruguay():
    """Prueba con todos los datos: calibra hasta 2020 y pronostica cada día de 2021 a hoy."""
    from . import uruguay as U
    D, B = _datos_uruguay("2011-01-01")
    R = _lluvia_historica("2011-01-01")
    U.evaluar(D, R, B)


def uruguay():
    """Pronóstico diario de prueba (no lo muestra el portal): guarda en sige_hidro_pron_uruguay."""
    from . import supa, uruguay as U
    filas = {r["id"]: r for r in supa.select("sige_hidro_modelo_uruguay", {"select": "id,datos"})}
    if "modelo" not in filas:
        print("uruguay: falta el modelo (python -m motor calibrar_uruguay)")
        return
    modelos = [U.normalizar(filas[i]["datos"]) for i in ("modelo", "modelo_sin_ana") if i in filas]
    hoy = dt.date.today()
    D, B = _datos_uruguay((hoy - dt.timedelta(days=90)).isoformat(), telemetria_dias=8)
    R = _lluvia_supa((hoy - dt.timedelta(days=90)).isoformat())
    rec = U.lluvia_reciente()
    supa.upsert("sige_hidro_lluvia", [{"zona": z, "fecha": f, "mm": round(v, 2), "tipo": "pronostico", "actualizado": hoy.isoformat()}
                                      for z, d in rec.items() for f, v in d.items()], "zona,fecha,tipo")
    for z, d in rec.items():   # ERA5 donde existe; los últimos días y el futuro, del modelo meteorológico
        for f, v in d.items():
            R.setdefault(z, {}).setdefault(f, v)
    P = None
    for M in modelos:   # primero con escalas de Brasil; si no respondieron, el modelo de respaldo
        for atras in range(0, 8):
            f = (hoy - dt.timedelta(days=atras)).isoformat()
            P = U.pronosticar(M, D, R, B, f)
            if P:
                break
        if P:
            print(f"uruguay: modelo {'con' if M.get('mej', {}).get('ana') else 'sin'} escalas de Brasil")
            break
    if not P:
        print("uruguay: faltan datos para pronosticar")
        return
    lluvia7 = sum(U.PESO_ZONA[z] * U._suma(R, z, f, 1, 7) for z in U.ZONAS)
    supa.upsert("sige_hidro_pron_uruguay", [{"emitido": hoy.isoformat(), "k": k, "dato_fecha": f, "fecha": v["fecha"],
                                             "q_entra": v["q_entra"], "q_sale": v["q_sale"], "h_concordia": v["conc"],
                                             "h_colon": v["colon"], "h_cdu": v["cdu"], "lluvia_fut_mm": round(lluvia7, 1)}
                                            for k, v in P.items()], "emitido,k")
    print(f"uruguay: desde {f} · lluvia pronosticada 7 días {lluvia7:.0f} mm · " + " · ".join(
        f"+{k}d entra {v['q_entra']} sale {v['q_sale']} Concordia {v['conc']}" for k, v in P.items() if k in (3, 7)))


COLUMNAS_CRUCE = ("nivel", "tipo", "umbral", "momento", "desde_rango", "hasta_rango", "sale", "en_nivel_desde",
                  "desde_antes_de_serie", "permanencia_h", "permanencia_minima")


def lluvia_cuencas():
    """Lluvia por cuenca (Open-Meteo) → sige_hidro_cuencas (una fila por cuenca; también corre con todo)."""
    from . import supa, cuencas as CU
    filas = CU.calcular()
    supa.upsert("sige_hidro_cuencas", filas, "cuenca")
    print(f"cuencas: {len(filas)} cuencas actualizadas")
    for r in sorted(filas, key=lambda r: -(r["pron_7d"] or 0))[:12]:
        print(f"  {r['nombre'][:40]:40s} llovió 7d {r['lluvia_7d']:6.1f} mm · pronóstico 7d {r['pron_7d']:6.1f} mm · "
              f"suelo {r['suelo']} · aporte {r['aporte_7d_m3s']} → {r['aporte_pron7d_m3s']} m³/s")


def mejorar():
    """Compara formas de pronosticar (años de calibración, represas y ríos de Brasil, Salto Grande, lluvia caída y
    pronosticada) con una prueba hacia atrás de los avisos de alerta / evacuación. No cambia el pronóstico del portal."""
    from . import revision, mejoras, calibrar as C
    est = estaciones()
    S = _series_largas(dt.date.today().year - 2009)
    S.update(mejoras.entradas("2009-01-01"))
    M = mejoras.meta()
    print("mejorar: entradas nuevas " + ", ".join(f"{M[k]['nombre']} {len(S[k])} d" for k in M if k in S), flush=True)
    ita = {C.ITAIPU_ID: "Itaipú (descarga)"}
    br = {k: {**m, "futuro": False} for k, m in M.items() if m["tipo"] != "lluvia" and k in S}
    ll = {k: {**m, "futuro": False} for k, m in M.items() if m["tipo"] == "lluvia" and k in S}
    llf = {k: {**m, "futuro": True} for k, m in M.items() if m["tipo"] == "lluvia" and k in S}
    variantes = [("3 años (como hoy)", 3, ita, 4), ("7 años", 7, ita, 4), ("7 años + Brasil y Salto Grande", 7, {**ita, **br}, 6),
                 ("+ lluvia caída", 7, {**ita, **br, **ll}, 6), ("+ lluvia pronosticada", 7, {**ita, **br, **llf}, 6)]
    hoy = dt.date.today() - dt.timedelta(days=20)
    folds = [(dt.date(2018, 12, 31), "2019-01-01", hoy.isoformat()), (hoy, "2011-06-01", "2018-12-31")]
    revision.comparar(est, S, variantes, folds, C)


def _calcular(S, modelo, sube_7d):
    emitido = dt.datetime.now(nucleo.ART).isoformat(timespec="minutes")
    pron, cru = [], []
    for e in estaciones():
        proy = nucleo.proyectar(S, modelo, e["id"], sube_7d)
        if not proy:
            continue
        pron += [{"estacion_id": e["id"], "emitido": emitido, "dato_fecha": max(S[e["id"]]), **q} for q in proy]
        for ev in (nucleo.cruces(S[e["id"]], proy, e["alerta"], e["evac"]) if e["alerta"] is not None and e["evac"] is not None else []):
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
        try:
            calibrar_glofas()
        except Exception as ex:   # el modelo de prueba nunca frena al normal
            print("calibrar_glofas: no se pudo:", ex)
        try:
            calibrar_uruguay()
        except Exception as ex:
            print("calibrar_uruguay: no se pudo:", ex)
    elif tarea in ("auditar", "auditar_borrar"):
        from . import revision
        revision.auditar(estaciones(), borrar=tarea == "auditar_borrar")
    elif tarea == "mejorar":
        mejorar()
    elif tarea == "cuencas":
        lluvia_cuencas()
    elif tarea == "evaluar_cruces":
        from . import revision
        from .calibrar import calibrar as cal, ITAIPU_ID
        corte = dt.date(2007, 12, 31)   # modelos de prueba calibrados antes de los años que se evalúan (2008-2023)
        S = _series_largas(dt.date.today().year - 1998)
        Sc = {k: {f: v for f, v in s.items() if f <= corte.isoformat()} for k, s in S.items()}
        extra = {f"calibrado hasta 2007 con {a} años": cal(estaciones(), Sc, corte, anios=a, log=lambda *x: None,
                                                          extras={ITAIPU_ID: "Itaipú (descarga)"}) for a in (3, 8)}
        revision.evaluar_cruces(estaciones(), lambda anios: S, extra=extra)
    elif tarea == "calibrar_glofas":
        calibrar_glofas()
    elif tarea == "prefectura":
        f = []
        lec = _prefectura(f)
        for l in lec[:12]:
            print("  ", l)
        print("fallos:", f)
    elif tarea == "calibrar_uruguay":
        calibrar_uruguay()
    elif tarea == "evaluar_uruguay":
        evaluar_uruguay()
    elif tarea == "uruguay":
        uruguay()
    elif tarea == "glofas":
        glofas_prueba()
    elif tarea == "evaluar_prueba":
        evaluar_prueba()
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
        try:
            glofas_prueba()
        except Exception as ex:   # modo prueba: si falla, no afecta al pronóstico del portal
            print("glofas: sin actualizar:", ex)
        try:
            uruguay()
        except Exception as ex:
            print("uruguay: sin actualizar:", ex)
        try:
            lluvia_cuencas()
        except Exception as ex:   # si la tabla todavía no existe o Open-Meteo no responde, lo demás ya quedó
            print("cuencas: sin actualizar:", ex)
