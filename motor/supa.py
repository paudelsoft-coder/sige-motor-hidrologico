"""Cliente mínimo de Supabase (PostgREST) sin dependencias.

Variables de entorno (en GitHub: Settings › Secrets and variables › Actions):
  SUPABASE_URL          https://<proyecto>.supabase.co
  SUPABASE_SERVICE_KEY  clave service_role (solo en el servidor; nunca en el portal)
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _req(method, path, body=None, headers=None, params=None):
    if not URL or not KEY:
        raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_SERVICE_KEY")
    q = ("?" + urllib.parse.urlencode(params, safe="(),.*:")) if params else ""
    h = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, default=str).encode() if body is not None else None
    req = urllib.request.Request(f"{URL}/rest/v1/{path}{q}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode() or "null"
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"Supabase {method} {path}: {ex.code} {ex.read().decode(errors='replace')[:500]}") from None
    return json.loads(txt)


def select(tabla, params=None, pagina=1000):
    """Lee todas las filas (paginando con Range)."""
    out, ini = [], 0
    while True:
        filas = _req("GET", tabla, params=params, headers={"Range-Unit": "items", "Range": f"{ini}-{ini + pagina - 1}"})
        out += filas
        if len(filas) < pagina:
            return out
        ini += pagina


def upsert(tabla, filas, conflicto, lote=500):
    for i in range(0, len(filas), lote):
        _req("POST", tabla, filas[i:i + lote], params={"on_conflict": conflicto},
             headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


def borrar(tabla, params):
    _req("DELETE", tabla, params=params, headers={"Prefer": "return=minimal"})


def insertar(tabla, filas, lote=500):
    for i in range(0, len(filas), lote):
        _req("POST", tabla, filas[i:i + lote], headers={"Prefer": "return=minimal"})
