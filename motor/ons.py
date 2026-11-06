"""Caudales diarios de represas brasileñas (Itaipú, cuenca del Iguazú y Alto Uruguay) desde los datos abiertos del ONS.

Fuente: ONS, "Dados Hidrológicos por Reservatório – Base diária" (dados.ons.org.br, dataset dados-hidrologicos-res).
Un CSV por año, separado por ';', con columnas din_instante, val_vazaoafluente, val_vazaoturbinada,
val_vazaovertida, val_vazaodefluente, val_nivelmontante (m³/s y m s. n. m.).
"""
import csv
import io
import time
import urllib.request

URL = "https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/dados_hidrologicos_di/DADOS_HIDROLOGICOS_RES_{anio}.csv"
UA = "SIGE-MotorHidrologico/1.0 (defensa civil; uso no comercial)"
# nombre en el ONS -> clave en sige_hidro_represas
REPRESAS = {"ITAIPU": "ITAIPU", "BAIXO IGUACU": "BAIXO_IGUACU", "SALTO CAXIAS": "SALTO_CAXIAS", "PORTO PRIMAVERA": "PORTO_PRIMAVERA",
            # Alto Uruguay (Brasil): avisan con 6 a 9 días lo que va a llegar a Salto Grande
            "FOZ CHAPECO": "FOZ_CHAPECO", "ITÁ": "ITA", "ITA": "ITA", "PASSO SAO JOAO": "PASSO_SAO_JOAO", "SAO JOSE": "SAO_JOSE",
            "MONJOLINHO": "MONJOLINHO"}


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def anio(a: int) -> list[dict]:
    req = urllib.request.Request(URL.format(anio=a), headers={"User-Agent": UA})
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                txt = r.read().decode("utf-8-sig", "replace")
            break
        except Exception:
            if intento == 2:
                raise
            time.sleep(10 * (intento + 1))
    filas = []
    for row in csv.DictReader(io.StringIO(txt), delimiter=";"):
        clave = REPRESAS.get((row.get("nom_reservatorio") or "").strip().upper())
        if not clave:
            continue
        filas.append({"represa": clave, "fecha": row["din_instante"][:10],
                      "afluente_m3s": _num(row.get("val_vazaoafluente")), "turbinado_m3s": _num(row.get("val_vazaoturbinada")),
                      "vertido_m3s": _num(row.get("val_vazaovertida")), "defluente_m3s": _num(row.get("val_vazaodefluente")),
                      "cota_m": _num(row.get("val_nivelmontante")), "fuente": "ONS"})
    return filas
