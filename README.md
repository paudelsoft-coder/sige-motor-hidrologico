# Motor hidrológico SIGE

Corre de fondo, sin costo, en GitHub Actions. Cada hora descarga las alturas del INA, calcula el pronóstico a 15 días de cada puerto y la fecha y hora estimadas en que entra o sale de alerta y evacuación. Deja todo en Supabase. El SIGE PORTAL solo lee el resultado (tabla `sige_hidro_cruces` y vista `sige_hidro_proximos_cruces`).

## Puesta en marcha (una sola vez)

1. **Supabase:** ejecutar `sql/2026-11-05_motor_hidrologico.sql` en el SQL Editor. Crea las tablas, las vistas, la lectura pública y carga las 33 estaciones.
2. **GitHub:** crear un repositorio privado (por ejemplo `sige-motor-hidrologico`) y subir esta carpeta completa.
3. **Secretos:** en el repositorio, *Settings › Secrets and variables › Actions*, agregar:
   - `SUPABASE_URL`: la URL del proyecto.
   - `SUPABASE_SERVICE_KEY`: la clave `service_role`. Solo va acá, nunca en el portal ni en SIGE.
4. **Primera carga:** en *Actions › Motor hidrológico SIGE › Run workflow*, elegir `primera-carga`. Baja 3 años de alturas, calibra y calcula el primer pronóstico (tarda unos minutos).
5. Desde ahí corre solo: cada hora (alturas, pronóstico y cruces) y los lunes (recalibración).

## Qué calcula

| Tarea | Resultado | Tabla |
|---|---|---|
| `ingesta` | Altura diaria de las 00 h de 33 puertos del Paraná y del Paraguay | `sige_hidro_alturas` |
| `calibrar` | Pesos de propagación desde aguas arriba y banda de error por puerto y por día | `sige_hidro_modelo` |
| `pronostico` | Altura día por día (15 días) y cruces de nivel con fecha y hora, rango y permanencia | `sige_hidro_pronostico`, `sige_hidro_cruces` |
| `represas` | Caudal diario de Itaipú y de las represas del Iguazú (descarga, turbinado, vertido, cota), datos abiertos del ONS | `sige_hidro_represas` |
| `transito` | Desfase y sensibilidad de cada puerto respecto de Itaipú y Yacyretá, y caudal a partir del cual Yacyretá vierte | `sige_hidro_transito` |

**Hora de cruce:** las alturas son de las 00 h y la proyección da un valor por día. Entre dos días se interpola para estimar el momento en que la altura cruza el nivel. La banda de error da el rango: lo más temprano y lo más tarde. Con datos horarios, el mismo cálculo da la hora con más precisión.

**Itaipú en el pronóstico:** la calibración usa la descarga diaria de Itaipú (ONS) como entrada de los puertos del Paraná. Probado con 2000–2026, baja el error a 7 días un 14–16 % en Corrientes, Paso de la Patria e Itatí; con 3 años (2023–2026), Corrientes pasa de 0,48 a 0,42 m. La descarga de Itaipú resultó la primera entrada elegida en esos puertos.

**Yacyretá:** el ONS no la publica y la EBY no tiene una serie diaria descargable; la altura de Ituzaingó (aguas abajo de la represa) funciona como su salida. La tabla `sige_hidro_represas` acepta YACYRETA para cargar sus partes cuando se consigan.

**Tránsito:** con la historia de `sige_hidro_represas` (al menos 120 días), `transito` calcula:
- en cuántos días llega un cambio de caudal a cada puerto y cuántos cm sube cada 1.000 m³/s;
- la velocidad de la onda en cada tramo, pasando los km del tramo a `transito.celeridad`;
- el tránsito por tramos (Muskingum);
- la regla de apertura del vertedero de Yacyretá.

## Cómo pronostica (código propio del SIGE)

- `calidad.py`: control de calidad de las alturas (ceros, tipeos, picos de un día, último dato imposible) con estadística robusta; devuelve las crecidas rápidas que confirma un puerto vecino.
- `modelo.py`: modelo "sige2" por puerto. Para cada horizonte (1 a 15 días) predice el cambio de altura con diferencias de las series de aguas arriba (puertos, represas de Brasil y Salto Grande, ríos de la ANA, lluvia caída y pronosticada por zona). Regresión elastic-net con validación cruzada por bloques; banda = cuantiles 10 % y 90 % del error fuera de muestra. Calibra con 7 años.
- `proyeccion.py`: proyección a 15 días y momento de cruce de alerta / evacuación.
- `analogos.py`: situaciones parecidas en la historia de cada puerto.
- `a5.py`: descarga de la API pública del Sistema de Alerta del INA.

Prueba hacia atrás (`python -m motor mejorar`, 2011–2026, dos períodos que el modelo no vio): en alerta detecta el 44 % de los días con cruce real en 15 días, con 37 % de avisos que no se cumplieron.

## Licencias y fuentes

- Alturas y niveles: INA, Alerta Hidrológico Cuenca del Plata; escalas de Prefectura Naval Argentina.
- Represas de Brasil: ONS (datos abiertos). Ríos de Brasil: ANA (HidroWeb). Salto Grande: CTM vía INA.
- Lluvia: Open-Meteo (ERA5 y modelos de pronóstico), CC BY 4.0.
- Todo el código de este repositorio es propio del SIGE.
