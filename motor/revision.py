"""Revisión de datos y prueba hacia atrás del pronóstico (no cambia nada del pronóstico del portal).

  auditar         → calidad de la historia de cada puerto (INA/PNA) y de las series del río Uruguay (INA, Salto
                    Grande, ANA Brasil): filas vacías, valores imposibles, picos de un día, huecos, y los picos más
                    altos de cada puerto comparados con los puertos vecinos y con las crecidas extraordinarias conocidas.
  auditar_borrar  → lo mismo y además borra las filas vacías (sin ninguna altura) de la base.
  evaluar_cruces  → pronostica cada día de 2008 a 2023 (años que el modelo actual NO usó para calibrarse) y compara
                    "días que faltan para entrar en alerta / evacuación" con lo que realmente pasó.
El informe sale en el registro de la corrida y en el resumen de GitHub Actions (pestaña Summary).
"""
import datetime as dt
import os

import numpy as np

from . import calidad
from .base import mover

# Crecidas extraordinarias conocidas (año del pico) para reconocer picos reales en la historia.
CRECIDAS = {
    "PARANA": {1905: "1905", 1966: "1966", 1977: "1977", 1982: "1982-83", 1983: "1982-83", 1992: "1992",
               1997: "1997-98", 1998: "1997-98", 2010: "2009-10", 2014: "2014", 2015: "2015-16", 2016: "2015-16",
               2019: "2019", 2023: "2023-24", 2024: "2023-24"},
    "URUGUAY": {1959: "1959", 1972: "1972", 1983: "1983", 1990: "1990", 1992: "1992", 1997: "1997-98",
                1998: "1997-98", 2002: "2002", 2009: "2009-10", 2010: "2009-10", 2014: "2014", 2015: "2015-16",
                2016: "2015-16", 2017: "2017", 2019: "2019", 2023: "2023-24", 2024: "2023-24"},
}
D0 = dt.date(1900, 1, 1)


class Informe:
    """Junta las líneas para el registro y para el resumen (markdown) de GitHub Actions."""
    def __init__(self, titulo):
        self.md = [f"# {titulo}", ""]
        print("=" * 100 + "\n" + titulo)

    def __call__(self, txt="", md=None):
        print(txt, flush=True)
        self.md.append(txt if md is None else md)

    def tabla(self, cab, filas):
        print("  ".join(f"{c:>10s}" if i else f"{c:28s}" for i, c in enumerate(cab)))
        for f in filas:
            print("  ".join(f"{str(c):>10s}" if i else f"{str(c)[:28]:28s}" for i, c in enumerate(f)), flush=True)
        self.md += ["", "| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
        self.md += ["| " + " | ".join(str(c) for c in f) + " |" for f in filas]
        self.md.append("")

    def guardar(self):
        ruta = os.environ.get("GITHUB_STEP_SUMMARY")
        if ruta:
            with open(ruta, "a", encoding="utf-8") as f:
                f.write("\n".join(self.md)[:200000] + "\n\n")


def _cuenca(e):
    return "URUGUAY" if e["rio"].startswith("Uruguay") else "PARANA"


def _vecinos(e, est, n=2):
    """Puertos del mismo río (aguas arriba y abajo) para confirmar picos."""
    mismo = [u for u in est if _cuenca(u) == _cuenca(e) and u["id"] != e["id"] and not u.get("marea")
             and (u["rio"].startswith("Paraguay") == e["rio"].startswith("Paraguay") or abs(u["orden"] - e["orden"]) < 1.5)]
    mismo.sort(key=lambda u: abs(u["orden"] - e["orden"]))
    return mismo[:n * 2]


def _clase(serie, f, v):
    """Tipo de lectura descartada: cero de registro vacío, imposible (muy lejos de sus vecinas) o pico de un día."""
    if v == 0:
        return "cero"
    fs = sorted(serie)
    i = fs.index(f)
    w = [serie[x] for x in fs[max(0, i - 5):i] + fs[i + 1:i + 6]]
    m = sorted(w)[len(w) // 2] if w else v
    return "imposible" if abs(v - m) > max(3.0, 0.5 * abs(m)) else "pico"


def _p(serie, q):
    v = sorted(serie.values())
    return v[min(len(v) - 1, int(q * (len(v) - 1)))] if v else None


def _picos_anuales(serie, n=6):
    """Máximo de cada año (fecha, valor), los `n` más altos."""
    por = {}
    for f, v in serie.items():
        a = int(f[:4])
        if a not in por or v > por[a][1]:
            por[a] = (f, v)
    return sorted(por.values(), key=lambda x: -x[1])[:n]


def _confirma(f, serie_vec, umbral, margen=20):
    """¿El puerto vecino también estuvo alto (≥ su percentil 97) dentro de ±`margen` días?"""
    if not serie_vec or umbral is None:
        return None
    hay = [serie_vec.get(mover(f, k)) for k in range(-margen, margen + 1)]
    hay = [x for x in hay if x is not None]
    if not hay:
        return None
    return max(hay) >= umbral


def auditar(est, borrar=False):
    from . import supa
    I = Informe("Revisión de la historia de alturas (INA / Prefectura)")
    nombre = {e["id"]: e["nombre"] for e in est}
    # --- filas vacías en la base
    vacias_alt = supa.select("sige_hidro_alturas", {"select": "estacion_id,fecha", "altura_m": "is.null"})
    S, vacias_hist, filas_hist = {}, [], 0
    for r in supa.select("sige_hidro_historia", {"select": "estacion_id,anio,valores"}, pagina=200):
        filas_hist += 1
        vals = r["valores"] or []
        if not any(v is not None for v in vals):
            vacias_hist.append((r["estacion_id"], r["anio"]))
            continue
        d0 = dt.date(r["anio"], 1, 1)
        dest = S.setdefault(r["estacion_id"], {})
        for i, v in enumerate(vals):
            if v is not None:
                dest[(d0 + dt.timedelta(days=i)).isoformat()] = float(v)
    for r in supa.select("sige_hidro_alturas", {"select": "estacion_id,fecha,altura_m", "altura_m": "not.is.null"}):
        S.setdefault(r["estacion_id"], {})[r["fecha"]] = float(r["altura_m"])
    I(f"Filas vacías: {len(vacias_alt)} en alturas diarias, {len(vacias_hist)} años sin ningún dato en la historia "
      f"(de {filas_hist} años-puerto).")
    if vacias_hist:
        I("  años vacíos: " + ", ".join(f"{nombre.get(k, k)} {a}" for k, a in vacias_hist[:60]))
    if borrar:
        for k, a in vacias_hist:
            supa.borrar("sige_hidro_historia", {"estacion_id": f"eq.{k}", "anio": f"eq.{a}"})
        if vacias_alt:
            supa.borrar("sige_hidro_alturas", {"altura_m": "is.null"})
        I(f"Borradas: {len(vacias_hist)} filas de historia y {len(vacias_alt)} de alturas (todas sin ningún valor).")
    # --- limpieza por puerto
    limpia, desc = {}, {}
    for k, s in S.items():
        limpia[k], desc[k] = calidad.limpiar_dos_pasos(s)
    vuelven = calidad.restituir(limpia, desc, est)
    I(f"Crecidas rápidas que la limpieza descartaba y ahora se devuelven (un puerto vecino las confirma): "
      f"{sum(len(v) for v in vuelven.values())} lecturas en {len(vuelven)} puertos.")
    filas, perdidas = [], []
    hoy = dt.date.today()
    for e in sorted(est, key=lambda x: x["orden"]):
        s = S.get(e["id"])
        if not s:
            filas.append([e["nombre"], "-", "-", 0, "-", "-", "-", "-", "-", "-"]); continue
        fs = sorted(s)
        malas = desc[e["id"]]
        cl = {"cero": 0, "imposible": 0, "pico": 0}
        for f, v in malas.items():
            cl[_clase(s, f, v)] += 1
        d10 = (hoy - dt.timedelta(days=3652)).isoformat()
        cob = sum(1 for f in fs if f >= d10) / 3652
        hueco, hf = 0, None
        prev = None
        for f in fs:
            if f < "2000-01-01":
                continue
            if prev:
                g = (dt.date.fromisoformat(f) - dt.date.fromisoformat(prev)).days - 1
                if g > hueco:
                    hueco, hf = g, prev
            prev = f
        filas.append([e["nombre"], fs[0][:4], fs[-1], len(s), f"{100 * cob:.0f}%", cl["cero"], cl["imposible"], cl["pico"],
                      f"{hueco} d" + (f" ({hf[:7]})" if hf and hueco > 30 else ""), f"{max(limpia[e['id']].values()):.2f}"])
        # lecturas descartadas que podrían ser crecida real: arriba de la alerta y un vecino también estaba alto
        if e["alerta"] is not None:
            for f, v in malas.items():
                if v >= e["alerta"] and v < (e["evac"] or v) * 1.6 and _clase(s, f, v) != "imposible":
                    ok = [u["nombre"] for u in _vecinos(e, est) if _confirma(f, limpia.get(u["id"]), _p(limpia.get(u["id"]) or {}, .97), 5)]
                    if ok:
                        perdidas.append(f"{e['nombre']} {f}: {v:.2f} m descartada, pero {', '.join(ok[:2])} también estaba alto")
    I("")
    I("Datos por puerto (descartadas = lecturas que el control de calidad deja afuera al calcular):", "## Datos por puerto")
    I.tabla(["puerto", "desde", "último", "días", "cob. 10 años", "ceros", "imposibles", "picos 1 día", "mayor hueco", "máximo"], filas)
    I("Lecturas altas descartadas que quizás eran reales (revisar a mano):" if perdidas else
      "Ninguna lectura alta descartada coincide con crecida en los puertos vecinos (la limpieza no se comió crecidas reales).",
      "### Lecturas altas descartadas a revisar" if perdidas else None)
    for x in perdidas[:80]:
        I("  - " + x)
    # --- picos extremos: ¿crecida real o error?
    I("", "## Picos más altos de cada puerto")
    I("Picos más altos (máximo de cada año, ya limpio). ✔ = un puerto vecino también estuvo en su 3 % más alto "
      "dentro de ±20 días; ★ = año de crecida extraordinaria conocida; ? = sin confirmar (revisar).")
    dudosos = []
    for e in sorted(est, key=lambda x: x["orden"]):
        s = limpia.get(e["id"])
        if not s or len(s) < 365:
            continue
        vec = _vecinos(e, est)
        txt = []
        for f, v in _picos_anuales(s):
            conf = [_confirma(f, limpia.get(u["id"]), _p(limpia.get(u["id"]) or {}, .97)) for u in vec]
            conf_ok = any(c for c in conf if c)
            sin_vec = all(c is None for c in conf)
            conocida = CRECIDAS[_cuenca(e)].get(int(f[:4]))
            marca = ("★" if conocida else "") + ("✔" if conf_ok else ("·" if sin_vec else "?"))
            txt.append(f"{f} {v:.2f} {marca}")
            if not conf_ok and not sin_vec and not conocida:
                dudosos.append(f"{e['nombre']} {f}: {v:.2f} m (alerta {e['alerta']}) — ningún vecino estuvo alto")
        I(f"  {e['nombre'][:26]:26s} " + " | ".join(txt))
    I("", "### Picos sin confirmar")
    I(f"Picos sin confirmar por los vecinos ni por una crecida conocida: {len(dudosos)}")
    for x in dudosos:
        I("  - " + x)
    I.guardar()
    _auditar_uruguay()


def _auditar_uruguay():
    """Series del río Uruguay que usa el modelo de Concordia: INA (Salto Grande, Concordia, Colón...) y ANA (Brasil)."""
    from . import ana, uruguay as U
    I = Informe("Revisión de las series del río Uruguay (INA · Salto Grande · ANA Brasil)")
    filas = []
    try:
        D, informe = U.series_ina("2011-01-01")
        for k, v in informe.items():
            s = D.get(k) or {}
            filas.append([f"INA {k}", min(s)[:10] if s else "-", max(s) if s else "-", len(s), v["imposibles"], v["picos"], v["congelados"]])
    except Exception as ex:
        I(f"INA sin datos: {ex}")
    try:
        for k, v in ana.series("2011-01-01", max_telemetria=0).items():
            s, inf = U.limpiar(v, 1, 60000, 15)
            filas.append([f"ANA {k}", min(s)[:10] if s else "-", max(s) if s else "-", len(s), inf["imposibles"], inf["picos"], inf["congelados"]])
    except Exception as ex:
        I(f"ANA sin datos: {ex}")
    I.tabla(["serie", "desde", "último", "días", "imposibles", "picos", "congelados"], filas)
    I.guardar()


# --------------------------------------------------------------------------------------------- prueba hacia atrás
def _arr(serie, d0, n):
    a = np.full(n, np.nan)
    for f, v in serie.items():
        i = (dt.date.fromisoformat(f) - d0).days
        if 0 <= i < n:
            a[i] = v
    return a


def _cambio(a, desde, hasta, tol=4):
    """Vector de h(t−desde) − h(t−hasta) para todo t, corriendo las dos puntas hasta `tol` días si falta el dato."""
    n = len(a)
    out = np.full(n, np.nan)
    for off in range(tol + 1):
        i1, i2 = np.arange(n) - desde - off, np.arange(n) - hasta - off
        ok = (i1 >= 0) & (i2 >= 0) & (i1 < n) & (i2 < n)   # ventanas hacia adelante (lluvia pronosticada)
        v = np.full(n, np.nan)
        v[ok] = a[i1[ok]] - a[i2[ok]]
        m = np.isnan(out) & ~np.isnan(v)
        out[m] = v[m]
    return out


def _cruce(v, L):
    """Primer momento (días, con fracción) en que la curva v[0..] pasa de < L a ≥ L; None si no cruza."""
    for k in range(1, len(v)):
        if v[k - 1] < L <= v[k]:
            return k - 1 + (L - v[k - 1]) / (v[k] - v[k - 1])
    return None


def _probar(est, modelo, A, n, t_ini, t_fin, d0, sube_7d):
    """Pronostica cada día con `modelo` y cuenta aciertos / falsas / perdidas por puerto y nivel."""
    out = {"ALERTA": [], "EVACUACION": []}
    for e in sorted(est, key=lambda x: x["orden"]):
        M = modelo.get(e["id"]) or modelo.get(str(e["id"]))
        a = A.get(e["id"])
        if not M or a is None or e["alerta"] is None or e["evac"] is None or not M.get("w"):
            continue
        X = []
        for en in M["entradas"]:
            u = A.get(int(en["id"]))
            X.append(_cambio(u, en["desde"], en["hasta"]) if u is not None else np.full(n, np.nan))
        X = np.array(X)
        if len(M["w"][0]) == len(M["entradas"]) + 1:     # modelo propio (sige2): b0 + coeficientes, banda por cuantiles
            Wm = np.array(M["w"])
            P = a[None, :] + Wm[:, :1] + Wm[:, 1:] @ X
            alto = lambda clave: np.array([q[1] if q else np.nan for q in (M.get(clave) or M.get("err") or [None] * 15)])
            err, errs = alto("err"), alto("err_sube")
        else:                                              # modelo anterior: pesos sobre las diferencias, banda ± RMSE
            P = a[None, :] + np.array(M["w"]) @ X
            err = np.array([x if x is not None else np.nan for x in (M.get("err") or [np.nan] * 15)])
            errs = np.array([x if x is not None else np.nan for x in (M.get("err_sube") or M.get("err") or [np.nan] * 15)])
        h7 = np.concatenate([np.full(7, np.nan), a[:-7]])
        sube = (a - h7) > sube_7d
        for L, niv in ((e["alerta"], "ALERTA"), (e["evac"], "EVACUACION")):
            r = {"puerto": e["nombre"], "id": e["id"], "orden": e["orden"], "rio": e["rio"], "marea": e.get("marea"), "ac": 0, "fa": 0, "pe": 0, "sd": 0, "errd": [], "po": 0, "ev": []}
            aviso = np.zeros(n, bool)
            for t in range(t_ini, t_fin):
                h0 = a[t]
                if np.isnan(h0) or h0 >= L:
                    continue
                obs = a[t:t + 19]
                ok = ~np.isnan(obs)
                if ok[:16].sum() < 12:
                    continue
                o = np.interp(np.arange(19), np.nonzero(ok)[0], obs[ok])
                real = _cruce(o[:16], L)
                if np.isnan(P[:, t]).any():   # sin pronóstico ese día (faltaba un dato de entrada)
                    if real is not None:
                        r["sd"] += 1
                    continue
                real_tarde = _cruce(o, L)
                pred = _cruce(np.concatenate([[h0], P[:, t]]), L)
                e_k = errs if sube[t] else err
                pmax = _cruce(np.concatenate([[h0], P[:, t] + np.nan_to_num(e_k)]), L)
                if pred is not None:
                    aviso[t] = True
                    if real_tarde is not None:
                        r["ac"] += 1
                        r["errd"].append((real_tarde - pred, real_tarde))
                    else:
                        r["fa"] += 1
                elif real is not None:
                    r["pe"] += 1
                if real is not None and pmax is not None:
                    r["po"] += 1
            for t in range(t_ini + 10, t_fin):   # eventos: entra al nivel después de ≥ 10 días por debajo
                if not np.isnan(a[t]) and a[t] >= L and np.all(np.nan_to_num(a[t - 10:t], nan=-99) < L) and not np.isnan(a[t - 1]):
                    j = 0
                    while j < 15 and aviso[t - 1 - j]:
                        j += 1
                    r["ev"].append(((d0 + dt.timedelta(days=t)).isoformat(), j))
            if r["ac"] + r["pe"] + r["fa"] + r["sd"]:
                out[niv].append(r)
    return out


def _total(R):
    ac = sum(r["ac"] for r in R); fa = sum(r["fa"] for r in R); pe = sum(r["pe"] for r in R); sd = sum(r["sd"] for r in R)
    errd = [x for r in R for x in r["errd"]]
    ant = [j for r in R for _, j in r["ev"]]
    po = sum(r["po"] for r in R)
    return {"eventos": len(ant), "aviso_med": float(np.median(ant)) if ant else 0, "aviso_3mas": sum(1 for j in ant if j >= 3),
            "sin_aviso": sum(1 for j in ant if j == 0), "detecta": 100 * ac / max(1, ac + pe), "falsas": 100 * fa / max(1, ac + fa),
            "posible": 100 * po / max(1, ac + pe), "sin_dato": sd, "sesgo": float(np.mean([x for x, _ in errd])) if errd else 0,
            "err": {k: float(np.mean([abs(x) for x, real in errd if lo < real <= hi])) if any(lo < real <= hi for _, real in errd) else None
                    for k, (lo, hi) in {"1-3": (0, 3), "4-7": (3, 7), "8-15": (7, 18)}.items()}}


def evaluar_cruces(est, series_largas, desde="2008-01-01", hasta=None, extra=None):
    """`extra` = {nombre: modelo} para comparar con el modelo en uso (calibrados ANTES de `desde`)."""
    from . import supa
    filas_m = supa.select("sige_hidro_modelo", {"select": "*"})
    calibrado = min((r.get("calibrado") or "9999") for r in filas_m)
    sube_7d = (filas_m[0].get("sube_7d") or 0.15) if filas_m else 0.15
    modelos = {f"en uso (calibrado {calibrado}, 3 años)": {r["estacion_id"]: r for r in filas_m}}
    modelos.update(extra or {})
    if hasta is None:   # solo días que el modelo en uso no vio al calibrarse (calibra con los 3 años previos)
        hasta = (dt.date.fromisoformat(calibrado) - dt.timedelta(days=365 * 3 + 20)).isoformat()
    I = Informe(f"Prueba hacia atrás de los avisos de alerta y evacuación ({desde} a {hasta}, fuera de calibración)")
    S = series_largas(dt.date.today().year - int(desde[:4]) + 1)
    d0 = dt.date.fromisoformat(desde) - dt.timedelta(days=30)
    n = (dt.date.fromisoformat(hasta) - d0).days + 16
    A = {k: _arr(s, d0, n) for k, s in S.items()}
    t_ini, t_fin = 30, n - 16
    I("Cada día se pronostica con lo que se sabía ese día y se compara con lo que pasó en los 15 días siguientes "
      "(solo días en que el puerto estaba por debajo del nivel). Ningún modelo vio estos años al calibrarse.")
    I("aviso típico = días seguidos de aviso antes de entrar (mediana de los eventos) · detecta = % de días con cruce real en "
      "15 días que tuvieron aviso · falsas = % de avisos que no se cumplieron · 'posible' = cruce dentro de la banda de error · "
      "sin dato = días con cruce real en que no se pudo pronosticar porque faltaba un dato de entrada.")
    res = {nom: _probar(est, M, A, n, t_ini, t_fin, d0, sube_7d) for nom, M in modelos.items()}
    for niv in ("ALERTA", "EVACUACION"):
        I("", f"## Comparación de modelos · {niv.lower()}")
        filas = []
        for nom in modelos:
            T = _total(res[nom][niv])
            fe = lambda k: f"{T['err'][k]:.1f}" if T["err"][k] is not None else "-"
            filas.append([nom, T["eventos"], f"{T['aviso_med']:.0f} d", T["aviso_3mas"], T["sin_aviso"], f"{T['detecta']:.0f}%",
                          f"{T['falsas']:.0f}%", f"{T['posible']:.0f}%", T["sin_dato"], fe("1-3"), fe("4-7"), fe("8-15"), f"{T['sesgo']:+.1f}"])
        I.tabla(["modelo", "eventos", "aviso típico", "avisó ≥3 d", "sin aviso", "detecta", "falsas", "con 'posible'", "sin dato",
                 "error 1-3 d", "error 4-7 d", "error 8-15 d", "sesgo"], filas)
    mejor = max(modelos, key=lambda nom: _total(res[nom]["ALERTA"])["detecta"] - 0.5 * _total(res[nom]["ALERTA"])["falsas"])
    I("", f"## Detalle por puerto · {mejor}")
    for niv in ("ALERTA", "EVACUACION"):
        filas = []
        for r in res[mejor][niv]:
            ant = [j for _, j in r["ev"]]
            filas.append([r["puerto"], len(ant), f"{np.median(ant):.0f} d" if ant else "-", sum(1 for j in ant if j == 0),
                          r["ac"], r["fa"], r["pe"], r["sd"], f"{100 * r['ac'] / max(1, r['ac'] + r['pe']):.0f}%",
                          f"{100 * r['fa'] / max(1, r['ac'] + r['fa']):.0f}%", f"{100 * r['po'] / max(1, r['ac'] + r['pe']):.0f}%"])
        I(f"{niv.capitalize()}:", f"### {niv.capitalize()}")
        I.tabla(["puerto", "eventos", "aviso típico", "sin aviso", "aciertos", "falsas", "perdidas", "sin dato", "detecta", "falsas %",
                 "con 'posible'"], filas)
    I("", "## Eventos (fecha de entrada y días de aviso continuo antes)")
    for niv in ("ALERTA", "EVACUACION"):
        for r in res[mejor][niv]:
            if r["ev"]:
                I(f"  - {r['puerto']} {niv.lower()}: " + ", ".join(f"{f} ({j} d)" for f, j in r["ev"][:14]))
    I.guardar()


REGIONES = [("Iguazú y Alto Paraná", lambda r: r["orden"] < 7),
            ("Paraná medio (Ituzaingó–Esquina)", lambda r: 7 <= r["orden"] <= 15.5 and not r["rio"].startswith("Paraguay")),
            ("Río Paraguay y Bermejo", lambda r: r["rio"].startswith("Paraguay")),
            ("Paraná inferior (La Paz–Baradero)", lambda r: 15.5 < r["orden"] <= 28.2),
            ("Delta y Gualeguay", lambda r: 28.2 < r["orden"] < 100),
            ("Alto Uruguay (El Soberbio–Santo Tomé)", lambda r: 100 <= r["orden"] < 102.5),
            ("Uruguay medio (Alvear–Mocoretá)", lambda r: 102.5 <= r["orden"] < 106.8),
            ("Bajo Uruguay (Salto Grande–Gualeguaychú)", lambda r: r["orden"] >= 106.8)]


def comparar(est, S, variantes, folds, cal_mod, guardar_mejor=None):
    """Prueba hacia atrás de varias formas de calibrar el modelo, con validación cruzada por períodos: cada modelo se
    calibra con unos años y se evalúa en otros que no vio (`folds` = [(hasta_calibración, prueba_desde, prueba_hasta)]).
    `variantes` = [(nombre, años, extras, máximo de entradas)]. Devuelve {nombre: modelo calibrado hasta el último fold}."""
    I = Informe("Mejoras del pronóstico: prueba hacia atrás de cada variante (alerta y evacuación)")
    I("Cada variante se calibra con un período y se prueba en otro que no vio: " +
      " · ".join(f"calibrado hasta {h} → probado {a} a {b}" for h, a, b in folds) + ". Se suman los dos.")
    I("aviso típico = días seguidos de aviso antes de entrar (mediana) · detecta = % de días con cruce real en 15 días que "
      "tuvieron aviso · falsas = % de avisos que no se cumplieron · puntaje = detecta − ½ falsas.")
    res, modelos = {}, {}
    for nom, anios, extras, maxe, *resto in variantes:
        cm = resto[1] if len(resto) > 1 else cal_mod   # cada variante puede usar otro calibrador (modelo nuevo)
        cm.MAX_ENTRADAS = maxe
        cm.PESO_CRECIDA = resto[0] if resto else 0.0
        R = {"ALERTA": [], "EVACUACION": []}
        for hasta, t0, t1 in folds:
            h = hasta.isoformat()
            Sc = {k: {f: v for f, v in s.items() if f <= h} for k, s in S.items()}
            M = cm.calibrar(est, Sc, hasta, anios=anios, log=lambda *a: None, extras=extras)
            modelos[nom] = M
            d0 = dt.date.fromisoformat(t0) - dt.timedelta(days=30)
            n = (dt.date.fromisoformat(t1) - d0).days + 16
            A = {k: _arr(s, d0, n) for k, s in S.items()}
            out = _probar(est, M, A, n, 30, n - 16, d0, 0.15)
            for niv in R:
                R[niv] += out[niv]
            print(f"comparar: {nom} · hasta {h}: {len(M)} modelos", flush=True)
        res[nom] = R
    cab = ["variante", "eventos", "aviso típico", "avisó ≥3 d", "sin aviso", "detecta", "falsas", "con 'posible'",
           "error 1-3 d", "error 4-7 d", "error 8-15 d", "puntaje"]
    punt = {}
    for niv in ("ALERTA", "EVACUACION"):
        I("", f"## Total · {niv.lower()}")
        filas = []
        for nom in res:
            T = _total(res[nom][niv])
            fe = lambda k: f"{T['err'][k]:.1f}" if T["err"][k] is not None else "-"
            p = T["detecta"] - 0.5 * T["falsas"]
            punt[nom] = punt.get(nom, 0) + p
            filas.append([nom, T["eventos"], f"{T['aviso_med']:.0f} d", T["aviso_3mas"], T["sin_aviso"], f"{T['detecta']:.0f}%",
                          f"{T['falsas']:.0f}%", f"{T['posible']:.0f}%", fe("1-3"), fe("4-7"), fe("8-15"), f"{p:.0f}"])
        I.tabla(cab, filas)
    for niv in ("ALERTA", "EVACUACION"):
        I("", f"## Por zona · {niv.lower()} (aviso típico · detecta · falsas)")
        filas = []
        for zona, cond in REGIONES:
            fila = [zona]
            for nom in res:
                T = _total([r for r in res[nom][niv] if cond(r)])
                fila.append(f"{T['aviso_med']:.0f} d · {T['detecta']:.0f}% · {T['falsas']:.0f}%" if T["eventos"] or T["detecta"] else "-")
            filas.append(fila)
        I.tabla(["zona"] + list(res), filas)
    mejor = max(punt, key=punt.get)
    I("", f"## Mejor variante: {mejor}")
    for niv in ("ALERTA", "EVACUACION"):
        filas = []
        for r in res[mejor][niv]:
            ant = [j for _, j in r["ev"]]
            filas.append([r["puerto"], len(ant), f"{np.median(ant):.0f} d" if ant else "-", sum(1 for j in ant if j == 0),
                          r["ac"], r["fa"], r["pe"], f"{100 * r['ac'] / max(1, r['ac'] + r['pe']):.0f}%",
                          f"{100 * r['fa'] / max(1, r['ac'] + r['fa']):.0f}%"])
        I(f"{niv.capitalize()}:", f"### {niv.capitalize()}")
        I.tabla(["puerto", "eventos", "aviso típico", "sin aviso", "aciertos", "falsas", "perdidas", "detecta", "falsas %"], filas)
    I("", "### Entradas que eligió cada puerto (mejor variante, último período)")
    for e in sorted(est, key=lambda x: x["orden"]):
        M = modelos[mejor].get(str(e["id"]))
        if M:
            I(f"  - {e['nombre']}: " + ", ".join(f"{x['nombre']} {x['desde']}/{x['hasta']}" for x in M["entradas"]))
    I.guardar()
    return modelos, mejor
