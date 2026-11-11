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


# Clave de cada tabla: al paginar, el orden tiene que ser total. Sin orden (o con empates, p. ej. solo por fecha) la base
# puede devolver las filas en otro orden en cada página y se saltean o repiten filas: así se "perdían" años enteros de
# historia de algunos puertos al calibrar.
CLAVES = {"sige_hidro_historia": "estacion_id,anio", "sige_hidro_alturas": "estacion_id,fecha",
          "sige_hidro_represas": "represa,fecha", "sige_hidro_lluvia": "zona,fecha,tipo", "sige_hidro_glofas": "punto_id,fecha",
          "sige_hidro_pron_prueba": "estacion_id,emitido,k", "sige_hidro_pronostico": "estacion_id,k",
          "sige_hidro_modelo": "estacion_id", "sige_hidro_modelo_glofas": "estacion_id", "sige_hidro_modelo_uruguay": "id",
          "sige_hidro_cuencas": "cuenca", "sige_hidro_prono_ina": "estacion_id,modelo,emitido,fecha,nivel",
          "sige_hidro_pron_archivo": "estacion_id,dato_fecha,k"}


def select(tabla, params=None, pagina=1000):
    """Lee todas las filas (paginando con Range, con orden total para no perder ni repetir filas entre páginas)."""
    params = dict(params or {})
    if tabla in CLAVES:
        params["order"] = (params["order"] + "," if params.get("order") else "") + CLAVES[tabla]
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
