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

from . import nucleo

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
        print(txt)
        self.md.append(txt if md is None else md)

    def tabla(self, cab, filas):
        print("  ".join(f"{c:>10s}" if i else f"{c:28s}" for i, c in enumerate(cab)))
        for f in filas:
            print("  ".join(f"{str(c):>10s}" if i else f"{str(c)[:28]:28s}" for i, c in enumerate(f)))
        self.md += ["", "| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
        self.md += ["| " + " | ".join(str(c) for c in f) + " |" for f in filas]
        self.md.append("")

    def guardar(self):
        ruta = os.environ.get("GITHUB_STEP_SUMMARY")
        if ruta:
            with open(ruta, "a", encoding="utf-8") as f:
                f.write("\n".join(self.md)[:900000] + "\n\n")


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
    hay = [serie_vec.get(nucleo.sumar_dias(f, k)) for k in range(-margen, margen + 1)]
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
        l0, _ = nucleo.depurar(s)
        limpia[k], desc[k] = nucleo.depurar(s, nucleo.salto_tipico(l0))
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
        ok = (i1 >= 0) & (i2 >= 0)
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


def evaluar_cruces(est, series_largas, desde="2008-01-01", hasta=None):
    from . import supa
    filas_m = supa.select("sige_hidro_modelo", {"select": "*"})
    modelo = {r["estacion_id"]: r for r in filas_m}
    calibrado = min((r.get("calibrado") or "9999") for r in filas_m)
    sube_7d = filas_m[0].get("sube_7d") or 0.15 if filas_m else 0.15
    if hasta is None:   # solo días que el modelo no vio al calibrarse (calibra con los 3 años previos)
        hasta = (dt.date.fromisoformat(calibrado) - dt.timedelta(days=365 * 3 + 20)).isoformat()
    I = Informe(f"Prueba hacia atrás de los avisos de alerta y evacuación ({desde} a {hasta}, fuera de calibración)")
    anios = dt.date.today().year - int(desde[:4]) + 1
    S = series_largas(anios)
    d0 = dt.date.fromisoformat(desde) - dt.timedelta(days=30)
    d1 = dt.date.fromisoformat(hasta)
    n = (d1 - d0).days + 16
    A = {k: _arr(s, d0, n) for k, s in S.items()}
    t_ini, t_fin = 30, (d1 - d0).days
    I(f"Modelo calibrado el {calibrado}. Cada día se pronostica con lo que se sabía ese día y se compara con lo que pasó "
      f"en los 15 días siguientes. Solo días en que el puerto estaba por debajo del nivel.")
    I("ACIERTO = avisó y entró · FALSA = avisó y no entró (ni 3 días después) · PERDIDA = entró sin aviso · "
      "error = diferencia entre el día anunciado y el real (+ = el aviso llegó antes de lo real, el río tardó más).")
    resumen = {"ALERTA": [], "EVACUACION": []}
    filas_por = {"ALERTA": [], "EVACUACION": []}
    eventos_txt = []
    for e in sorted(est, key=lambda x: x["orden"]):
        M = modelo.get(e["id"])
        a = A.get(e["id"])
        if not M or a is None or e["alerta"] is None or e["evac"] is None or not M.get("w"):
            continue
        X = []
        for en in M["entradas"]:
            u = A.get(int(en["id"]))
            X.append(_cambio(u, en["desde"], en["hasta"]) if u is not None else np.full(n, np.nan))
        X = np.array(X)
        W = np.array(M["w"])              # 15 × entradas
        P = a[None, :] + W @ X            # 15 × n : central para k = 1..15
        h7 = np.concatenate([np.full(7, np.nan), a[:-7]])
        sube = (a - h7) > sube_7d
        err = np.array([x if x is not None else np.nan for x in (M.get("err") or [np.nan] * 15)])
        errs = np.array([x if x is not None else np.nan for x in (M.get("err_sube") or M.get("err") or [np.nan] * 15)])
        for L, niv in ((e["alerta"], "ALERTA"), (e["evac"], "EVACUACION")):
            ac, fa, pe, cn, errd, posib_ok, posib_fa = 0, 0, 0, 0, [], 0, 0
            aviso = np.zeros(n, bool)
            for t in range(t_ini, t_fin):
                h0 = a[t]
                if np.isnan(h0) or h0 >= L or np.isnan(P[:, t]).any():
                    continue
                obs = a[t:t + 19]
                ok = ~np.isnan(obs)
                if ok[:16].sum() < 12:
                    continue
                o = np.interp(np.arange(19), np.nonzero(ok)[0], obs[ok])
                real = _cruce(o[:16], L)
                real_tarde = _cruce(o, L)
                c = np.concatenate([[h0], P[:, t]])
                pred = _cruce(c, L)
                e_k = errs if sube[t] else err
                pmax = _cruce(np.concatenate([[h0], P[:, t] + np.nan_to_num(e_k)]), L)
                if pred is not None:
                    aviso[t] = True
                    if real_tarde is not None:
                        ac += 1
                        errd.append((real_tarde - pred, real_tarde))
                    else:
                        fa += 1
                elif real is not None:
                    pe += 1
                else:
                    cn += 1
                if real is not None and pmax is not None:
                    posib_ok += 1
                if pmax is not None and real_tarde is None:
                    posib_fa += 1
            # eventos: entra al nivel después de ≥ 10 días por debajo; ¿con cuántos días de anticipación avisó?
            ev = []
            for t in range(t_ini + 10, t_fin):
                if not np.isnan(a[t]) and a[t] >= L and np.all(np.nan_to_num(a[t - 10:t], nan=-99) < L) and not np.isnan(a[t - 1]):
                    j = 0
                    while j < 15 and aviso[t - 1 - j]:
                        j += 1
                    ev.append(((d0 + dt.timedelta(days=t)).isoformat(), j))
            tot_real = ac + pe
            if tot_real + fa == 0:
                continue
            mae = lambda z: f"{np.mean([abs(x) for x, _ in z]):.1f}" if z else "-"
            sesgo = f"{np.mean([x for x, _ in errd]):+.1f}" if errd else "-"
            cerca = [x for x in errd if x[1] <= 3]
            medio = [x for x in errd if 3 < x[1] <= 7]
            lejos = [x for x in errd if x[1] > 7]
            ant = [j for _, j in ev]
            filas_por[niv].append([e["nombre"], len(ev), f"{np.median(ant):.0f} d" if ant else "-",
                                   sum(1 for j in ant if j == 0), ac, fa, pe,
                                   f"{100 * ac / max(1, ac + pe):.0f}%", f"{100 * fa / max(1, ac + fa):.0f}%",
                                   mae(cerca), mae(medio), mae(lejos), sesgo,
                                   f"{100 * posib_ok / max(1, tot_real):.0f}%"])
            resumen[niv].append((ac, fa, pe, errd, ant, posib_ok, tot_real))
            if ev:
                eventos_txt.append(f"{e['nombre']} {niv.lower()}: " + ", ".join(f"{f} ({j} d)" for f, j in ev[:12]))
    cab = ["puerto", "eventos", "aviso típico", "sin aviso", "aciertos", "falsas", "perdidas", "detecta", "falsas %",
           "error 1-3 d", "error 4-7 d", "error 8-15 d", "sesgo", "con 'posible'"]
    for niv in ("ALERTA", "EVACUACION"):
        I("", f"## {niv.capitalize()}")
        I.tabla(cab, filas_por[niv])
        R = resumen[niv]
        if R:
            ac = sum(r[0] for r in R); fa = sum(r[1] for r in R); pe = sum(r[2] for r in R)
            errd = [x for r in R for x in r[3]]
            ant = [j for r in R for j in r[4]]
            po = sum(r[5] for r in R); tr = sum(r[6] for r in R)
            por_lead = {}
            for x, real in errd:
                por_lead.setdefault(min(15, int(np.ceil(real))), []).append(abs(x))
            I(f"TOTAL {niv}: {len(ant)} eventos · aviso típico {np.median(ant) if ant else 0:.0f} días antes "
              f"(25 %: {np.percentile(ant, 25) if ant else 0:.0f} · 75 %: {np.percentile(ant, 75) if ant else 0:.0f}) · "
              f"{sum(1 for j in ant if j == 0)} sin ningún aviso previo · detecta {100 * ac / max(1, ac + pe):.0f}% de los días con "
              f"cruce real · falsas {100 * fa / max(1, ac + fa):.0f}% · con 'posible' detecta {100 * po / max(1, tr):.0f}% · "
              f"sesgo {np.mean([x for x, _ in errd]) if errd else 0:+.2f} días")
            I("Error medio del día anunciado según cuántos días faltaban de verdad: " +
              ", ".join(f"{k} d → {np.mean(v):.1f}" for k, v in sorted(por_lead.items())))
    I("", "## Eventos (fecha de entrada y días de aviso continuo antes)")
    for x in eventos_txt:
        I("  - " + x)
    I.guardar()
