"""Utilidades de fechas del motor SIGE (código propio).

Las series son diccionarios {fecha ISO 'AAAA-MM-DD': valor}; el valor diario es la lectura de las 00 h de Argentina
(o el máximo del día en los puertos con marea).
"""
import datetime as dt

ART = dt.timezone(dt.timedelta(hours=-3))
UNA_HORA = 3600.0


def mover(fecha, dias):
    """'2026-11-11', 3 → '2026-11-14'."""
    return (dt.date.fromisoformat(fecha) + dt.timedelta(days=dias)).isoformat()


def entre(a, b):
    """Días de la fecha a a la fecha b (b − a)."""
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def medianoche(fecha):
    """00 h de Argentina del día `fecha`, con zona horaria."""
    d = dt.date.fromisoformat(fecha)
    return dt.datetime(d.year, d.month, d.day, tzinfo=ART)


def valor_cercano(serie, fecha, tolerancia=1):
    """Valor del día o, si falta, del día más cercano dentro de ±tolerancia (primero hacia atrás)."""
    for k in range(tolerancia + 1):
        for f in ((fecha,) if k == 0 else (mover(fecha, -k), mover(fecha, k))):
            if f in serie:
                return serie[f]
    return None
