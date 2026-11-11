"""Open-Meteo: uso gratuito (no comercial) o con suscripción comercial.

Si existe la variable de entorno OPENMETEO_KEY (en GitHub: Settings › Secrets and variables › Actions), todos los
pedidos van a los servidores comerciales (customer-*.open-meteo.com) con la clave; si no, a los gratuitos.
Para la lluvia histórica (ERA5) hace falta el plan "API Professional" (el Standard no incluye la API histórica).
"""
import os


def firmar(url: str) -> str:
    clave = os.environ.get("OPENMETEO_KEY", "").strip()
    if not clave or "open-meteo.com" not in url:
        return url
    for libre, pago in (("https://api.open-meteo.com", "https://customer-api.open-meteo.com"),
                        ("https://archive-api.open-meteo.com", "https://customer-archive-api.open-meteo.com"),
                        ("https://flood-api.open-meteo.com", "https://customer-flood-api.open-meteo.com"),
                        ("https://ensemble-api.open-meteo.com", "https://customer-ensemble-api.open-meteo.com")):
        if url.startswith(libre):
            url = pago + url[len(libre):]
            break
    return url + ("&" if "?" in url else "?") + "apikey=" + clave
