"""Calibración del modelo de pronóstico de alturas "sige2" (código propio del SIGE).

Para cada puerto se ajusta, de una sola vez para los 15 horizontes, una regresión con penalización elastic net
(mezcla de lasso y ridge) sobre diferencias recientes de muchas series candidatas:
  - el propio puerto y los puertos de aguas arriba de la misma cuenca,
  - entradas externas (represas y ríos de Brasil, Salto Grande, lluvia acumulada; ver mejoras.py),
  - ventanas: último día, últimos 3 días, días 3 a 7 y días 7 a 14 atrás (y, si la serie trae pronóstico, los
    próximos 3, 7 y 14 días).
La penalización deja en cero las entradas que no ayudan: el modelo elige solo qué series y qué ventanas usar.
La fuerza de la penalización se elige con validación cruzada por bloques de tiempo (cada bloque se pronostica con un
modelo que no lo vio). Con esos mismos errores fuera de muestra se arma la banda: cuantiles 10 % y 90 % del error,
en general y en días de río subiendo. Un puerto recibe modelo solo si le gana a suponer que el río no cambia.
Con PESO_CRECIDA > 0 los días de aguas altas (cerca de la alerta o subiendo) pesan más al ajustar y al elegir.
Salida: el formato que usa proyeccion.py.
"""
import datetime as dt

import numpy as np

H = 15
DIFS = [(0, 1), (0, 3), (3, 7), (7, 14)]
DIFS_FUTURO = [(-3, 0), (-7, 0), (-14, 0)]
VECINDAD = 9            # cuántas posiciones de aguas arriba se miran
PRESELECCION = 32       # columnas candidatas que pasan al ajuste (las más correlacionadas con lo que se quiere predecir)
MAX_ENTRADAS = 12       # tope de entradas que quedan en el modelo final
ALFAS = [0.3, 0.12, 0.05, 0.02, 0.008, 0.003]
MEZCLA_L1 = 0.7
BLOQUES = 4
GANANCIA_MIN = 0.02     # tiene que bajar al menos 2 % el error de "el río no cambia"
PESO_CRECIDA = 0.0
SUBE_7D = 0.15
ITAIPU_ID = 1000


def cuenca(e):
    return "URUGUAY" if e["rio"].startswith("Uruguay") else "PARANA"


def serie_diaria(serie, d0, d1):
    """Arreglo de un valor por día entre d0 y d1; huecos de hasta 5 días se completan en línea recta."""
    n = (d1 - d0).days + 1
    a = np.full(n, np.nan)
    for f, v in serie.items():
        i = (dt.date.fromisoformat(f) - d0).days
        if 0 <= i < n and v is not None:
            a[i] = v
    ok = np.nonzero(~np.isnan(a))[0]
    for i, j in zip(ok, ok[1:]):
        if 1 < j - i <= 6:
            a[i + 1:j] = np.interp(np.arange(i + 1, j), [i, j], [a[i], a[j]])
    return a


def _corrido(a, k):
    """b[i] = a[i − k] (k > 0 mira al pasado, k < 0 al futuro); NaN fuera del arreglo."""
    b = np.full(len(a), np.nan)
    if k >= 0:
        b[k:] = a[:len(a) - k] if k else a
    else:
        b[:k] = a[-k:]
    return b


def _ajuste(G, C, alfa, B=None, iteraciones=300, tol=1e-6):
    """Descenso por coordenadas del elastic net en forma de covarianzas (todas las salidas a la vez).
    G = ZᵀWZ/ΣW (p×p), C = ZᵀWY/ΣW (p×H) con Z estandarizada; devuelve B (p×H)."""
    p = G.shape[0]
    B = np.zeros((p, C.shape[1])) if B is None else B.copy()
    l1, l2 = alfa * MEZCLA_L1, alfa * (1 - MEZCLA_L1)
    for _ in range(iteraciones):
        cambio = 0.0
        for j in range(p):
            rho = C[j] - G[j] @ B + G[j, j] * B[j]
            nuevo = np.sign(rho) * np.maximum(np.abs(rho) - l1, 0) / (G[j, j] + l2)
            cambio = max(cambio, float(np.max(np.abs(nuevo - B[j]))))
            B[j] = nuevo
        if cambio < tol:
            break
    return B


class _Datos:
    """Matrices de un puerto: X (filas = días, columnas = diferencias), Y (cambio de altura a k días), pesos."""
    def __init__(self, X, Y, peso, sube):
        self.X, self.Y, self.peso, self.sube = X, Y, peso, sube

    def momentos(self, filas):
        w = self.peso[filas] / self.peso[filas].sum()
        X, Y = self.X[filas], self.Y[filas]
        mx, my = w @ X, w @ Y
        sd = np.sqrt(w @ (X - mx) ** 2)
        sd[sd < 1e-9] = 1.0
        Z = (X - mx) / sd
        Yc = Y - my
        return (Z * w[:, None]).T @ Z, (Z * w[:, None]).T @ Yc, mx, sd, my

    def predecir(self, filas, B, mx, sd, my):
        return ((self.X[filas] - mx) / sd) @ B + my


def _bloques(n):
    bordes = np.linspace(0, n, BLOQUES + 1).astype(int)
    for a, b in zip(bordes, bordes[1:]):
        prueba = np.arange(a, b)
        entreno = np.r_[np.arange(0, max(0, a - H)), np.arange(min(n, b + H), n)]
        yield entreno, prueba


def _validar(D, alfas):
    """Error fuera de muestra (n × H) para cada alfa, con validación cruzada por bloques."""
    n = len(D.Y)
    E = {a: np.full(D.Y.shape, np.nan) for a in alfas}
    for entreno, prueba in _bloques(n):
        if len(entreno) < 200 or not len(prueba):
            continue
        G, C, mx, sd, my = D.momentos(entreno)
        B = None
        for a in alfas:          # de más penalizado a menos: cada ajuste arranca del anterior
            B = _ajuste(G, C, a, B)
            E[a][prueba] = D.predecir(prueba, B, mx, sd, my) - D.Y[prueba]
    return E


def _puntaje(E, peso, hasta=10):
    ok = ~np.isnan(E[:, 0])
    if ok.sum() < 150:
        return None
    w = peso[ok]
    return float(np.mean(np.sqrt((w[:, None] * E[ok, :hasta] ** 2).sum(0) / w.sum())))


def _bandas(E, filas, minimo):
    if filas.sum() < minimo:
        return None
    out = []
    for k in range(H):
        r = -E[filas, k]                       # lo observado menos lo pronosticado
        r = r[~np.isnan(r)]
        out.append([round(float(np.percentile(r, 10)), 3), round(float(np.percentile(r, 90)), 3)] if len(r) >= minimo else None)
    return out


def calibrar(estaciones, series, hasta, anios=3, log=print, extras=None):
    """{id: modelo sige2} para los puertos con un modelo mejor que la persistencia.
    `extras` = {id: nombre} (solo puertos del Paraná, ventanas al pasado) o {id: {nombre, cuenca, futuro}}."""
    est = [e for e in estaciones if not e.get("marea")]
    extras = {k: v for k, v in (extras or {}).items() if k in series}
    d0 = hasta - dt.timedelta(days=365 * anios)
    A = {e["id"]: serie_diaria(series.get(e["id"], {}), d0, hasta) for e in est}
    A.update({k: serie_diaria(series[k], d0, hasta + dt.timedelta(days=15)) [: (hasta - d0).days + 1] for k in extras})
    nombre = {e["id"]: e["nombre"] for e in est}
    nombre.update({k: (v["nombre"] if isinstance(v, dict) else v) for k, v in extras.items()})
    n = (hasta - d0).days + 1
    modelo = {}
    for e in sorted(est, key=lambda x: x["orden"]):
        h = A[e["id"]]
        if np.isnan(h).mean() > 0.4:
            log(f"{e['nombre']}: pocos datos, sin modelo"); continue
        # columnas candidatas
        cols = []
        for u in est:
            if cuenca(u) == cuenca(e) and u["orden"] <= e["orden"] and e["orden"] - u["orden"] <= VECINDAD \
                    and np.isnan(A[u["id"]]).mean() < 0.4:
                cols += [(u["id"], a, b) for a, b in DIFS]
        for k, x in extras.items():
            if np.isnan(A[k]).mean() >= 0.4:
                continue
            if isinstance(x, dict):
                if x.get("cuenca") == cuenca(e):
                    cols += [(k, a, b) for a, b in DIFS + (DIFS_FUTURO if x.get("futuro") else [])]
            elif e["rio"].startswith("Paraná"):
                cols += [(k, a, b) for a, b in DIFS]
        t = np.arange(15, n - H)
        X = np.stack([_corrido(A[s], a)[t] - _corrido(A[s], b)[t] for s, a, b in cols], 1)
        Y = np.stack([h[t + k] - h[t] for k in range(1, H + 1)], 1)
        filas = ~np.isnan(X).any(1) & ~np.isnan(Y).any(1) & ~np.isnan(h[t])
        if filas.sum() < 400:
            log(f"{e['nombre']}: pocos días completos, sin modelo"); continue
        X, Y, tt = X[filas], Y[filas], t[filas]
        sube = (h[tt] - h[tt - 7]) > SUBE_7D
        peso = np.ones(len(Y))
        if PESO_CRECIDA and e.get("alerta") is not None:
            alto = (h[tt] >= e["alerta"] - max(0.5, 0.2 * e["alerta"])) | sube
            peso = np.where(alto, 1.0 + PESO_CRECIDA, 1.0)
        # preselección: las columnas más relacionadas con el cambio a 3 y a 7 días (siempre el propio puerto)
        Xc = (X - X.mean(0)) / (X.std(0) + 1e-9)
        corr = np.abs(Xc.T @ ((Y[:, [2, 6]] - Y[:, [2, 6]].mean(0)) / (Y[:, [2, 6]].std(0) + 1e-9))) / len(Y)
        orden = np.argsort(-corr.max(1))
        propias = [i for i, c in enumerate(cols) if c[0] == e["id"]]
        elegidas = list(dict.fromkeys(propias + [int(i) for i in orden[:PRESELECCION]]))
        D = _Datos(X[:, elegidas], Y, peso, sube)
        E = _validar(D, ALFAS)
        puntajes = {a: _puntaje(E[a], peso) for a in ALFAS}
        puntajes = {a: s for a, s in puntajes.items() if s is not None}
        if not puntajes:
            log(f"{e['nombre']}: no se pudo validar, sin modelo"); continue
        alfa = min(puntajes, key=puntajes.get)
        # ajuste final con todos los días; si quedan muchas entradas, se queda con las más fuertes y reajusta
        G, C, mx, sd, my = D.momentos(np.arange(len(Y)))
        B = _ajuste(G, C, alfa)
        fuerza = np.sqrt((B ** 2).sum(1))
        vivas = [i for i in np.argsort(-fuerza) if fuerza[i] > 1e-6][:MAX_ENTRADAS]
        if not vivas:
            log(f"{e['nombre']}: ninguna entrada sirve, sin modelo"); continue
        vivas = sorted(vivas)
        D2 = _Datos(D.X[:, vivas], Y, peso, sube)
        E2 = _validar(D2, [alfa])[alfa]
        s_mod, s_pers = _puntaje(E2, peso), _puntaje(-Y, peso)
        if s_mod is None or s_mod > s_pers * (1 - GANANCIA_MIN):
            log(f"{e['nombre']}: no le gana a la persistencia, sin modelo"); continue
        G, C, mx, sd, my = D2.momentos(np.arange(len(Y)))
        B = _ajuste(G, C, alfa)
        coef = B / sd[:, None]                                     # a unidades originales
        b0 = my - mx @ coef
        cols_fin = [cols[elegidas[i]] for i in vivas]
        modelo[str(e["id"])] = {
            "entradas": [{"id": s, "nombre": nombre[s], "desde": a, "hasta": b} for s, a, b in cols_fin],
            "w": [[round(float(b0[k]), 4)] + [round(float(c), 5) for c in coef[:, k]] for k in range(H)],
            "err": _bandas(E2, ~np.isnan(E2[:, 0]), 50),
            "err_sube": _bandas(E2, ~np.isnan(E2[:, 0]) & sube, 25),
        }
        log(f"{e['nombre']}: {len(cols_fin)} entradas, error a 7 días {np.sqrt(np.nanmean(E2[:, 6] ** 2)):.3f} m "
            f"(sin modelo {np.sqrt(np.mean(Y[:, 6] ** 2)):.3f} m)")
    return modelo
