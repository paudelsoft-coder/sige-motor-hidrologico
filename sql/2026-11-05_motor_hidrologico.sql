-- Motor hidrológico SIGE · tablas en Supabase
-- Escribe solo el motor (clave service_role desde GitHub Actions). El portal y SIGE leen con la clave anon.
-- Los datos son públicos (INA / Prefectura / represas), por eso la lectura es abierta.

create table if not exists public.sige_hidro_estaciones (
  id int primary key, nombre text not null, rio text, provincia text,
  lat double precision, lon double precision,
  alerta_m numeric, evacuacion_m numeric, orden numeric, serie_ina int, marea boolean default false
);

create table if not exists public.sige_hidro_alturas (
  estacion_id int not null, fecha date not null, altura_m numeric not null,
  fuente text default 'INA', cargado timestamptz default now(),
  primary key (estacion_id, fecha)
);

create table if not exists public.sige_hidro_modelo (
  estacion_id int primary key, calibrado date not null, sube_7d numeric default 0.15,
  entradas jsonb not null, w jsonb not null, err jsonb, err_sube jsonb
);

-- Proyección día por día (se reemplaza en cada corrida)
create table if not exists public.sige_hidro_pronostico (
  estacion_id int not null, emitido timestamptz not null, dato_fecha date not null,
  k int not null, fecha date not null, central numeric, min numeric, max numeric, sube boolean,
  primary key (estacion_id, k)
);

-- Cruces de nivel con fecha y hora estimadas (lo que muestra el portal)
create table if not exists public.sige_hidro_cruces (
  id bigint generated always as identity primary key,
  estacion_id int not null, emitido timestamptz not null,
  nivel text not null check (nivel in ('ALERTA','EVACUACION')),
  tipo text not null check (tipo in ('ENTRA','SALE','POSIBLE')),
  umbral numeric, momento timestamptz, desde_rango timestamptz, hasta_rango timestamptz,
  sale timestamptz, en_nivel_desde timestamptz, desde_antes_de_serie boolean,
  permanencia_h numeric, permanencia_minima boolean
);

-- Represas: caudales diarios (m³/s) y cota del embalse (m s. n. m.)
create table if not exists public.sige_hidro_represas (
  represa text not null,   -- ITAIPU, BAIXO_IGUACU, SALTO_CAXIAS, PORTO_PRIMAVERA, YACYRETA, SALTO_GRANDE
  fecha date not null, afluente_m3s numeric, defluente_m3s numeric, turbinado_m3s numeric,
  vertido_m3s numeric, cota_m numeric, fuente text, cargado timestamptz default now(),
  primary key (represa, fecha)
);

-- Tránsito calibrado: desfase y sensibilidad de cada puerto respecto de cada represa
create table if not exists public.sige_hidro_transito (
  origen text not null, estacion_id int not null, desfase int, correlacion numeric, cm_por_1000 numeric,
  calibrado timestamptz default now(), primary key (origen, estacion_id)
);

create table if not exists public.sige_hidro_corridas (
  id bigint generated always as identity primary key, tarea text, filas int, fallos jsonb, momento timestamptz default now()
);

-- Vista para el portal: próximos cruces ordenados por momento
create or replace view public.sige_hidro_proximos_cruces as
  select c.*, e.nombre, e.rio, e.provincia, e.lat, e.lon
  from public.sige_hidro_cruces c join public.sige_hidro_estaciones e on e.id = c.estacion_id
  where c.tipo in ('ENTRA','SALE') and (c.momento is null or c.momento > now() - interval '1 day')
  order by c.momento nulls last;

-- Vista para el portal: última altura de cada puerto
create or replace view public.sige_hidro_ultima_altura as
  select distinct on (a.estacion_id) a.estacion_id, a.fecha, a.altura_m, e.nombre, e.alerta_m, e.evacuacion_m
  from public.sige_hidro_alturas a join public.sige_hidro_estaciones e on e.id = a.estacion_id
  order by a.estacion_id, a.fecha desc;

do $$ declare t text; begin
  foreach t in array array['sige_hidro_estaciones','sige_hidro_alturas','sige_hidro_modelo','sige_hidro_pronostico',
                           'sige_hidro_cruces','sige_hidro_represas','sige_hidro_transito','sige_hidro_corridas'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists lectura_publica on public.%I', t);
    execute format('create policy lectura_publica on public.%I for select to anon, authenticated using (true)', t);
  end loop;
end $$;

-- Tiempo real para el portal
do $$ begin
  begin alter publication supabase_realtime add table public.sige_hidro_cruces; exception when others then null; end;
  begin alter publication supabase_realtime add table public.sige_hidro_alturas; exception when others then null; end;
end $$;

-- Estaciones del INA (escalas de Prefectura Naval) con niveles oficiales de alerta y evacuación
insert into public.sige_hidro_estaciones (id,nombre,rio,provincia,lat,lon,alerta_m,evacuacion_m,orden,serie_ina,marea) values
(9,'Puerto Iguazú','Paraná','Misiones',-25.58333,-54.56667,25.0,28.0,1.0,9,false),
(10,'Libertad','Paraná','Misiones',-25.91668,-54.62113,29.0,31.0,2.0,10,false),
(11,'Eldorado','Paraná','Misiones',-26.38333,-54.7,23.0,25.0,3.0,11,false),
(12,'Puerto Libertador','Paraná','Misiones',-26.8,-55.03333,14.5,15.5,4.0,12,false),
(13,'Santa Ana','Paraná','Misiones',-27.33509,-55.58841,9.3,9.8,5.0,13,false),
(14,'Posadas','Paraná','Misiones',-27.36667,-55.88333,11.0,12.0,6.0,14,false),
(15,'Ituzaingó','Paraná','Corrientes',-27.58333,-56.7,3.5,4.0,7.0,15,false),
(16,'Itá Ibaté','Paraná','Corrientes',-27.42336,-57.33327,7.0,7.5,8.0,16,false),
(17,'Itatí','Paraná','Corrientes',-27.26678,-58.24407,6.8,7.5,9.0,17,false),
(18,'Paso de la Patria','Paraná','Corrientes',-27.33333,-58.58333,6.5,7.0,10.0,18,false),
(55,'Puerto Pilcomayo','Paraguay','Formosa',-25.36667,-57.65,5.35,6.0,10.1,55,false),
(57,'Formosa','Paraguay','Formosa',-26.18333,-58.18333,7.8,8.3,10.2,57,false),
(19,'Corrientes','Paraná','Corrientes',-27.46364,-58.83887,6.5,7.0,11.0,19,false),
(20,'Barranqueras (Resistencia)','Paraná (riacho Barranqueras)','Chaco',-27.48333,-58.93333,6.0,6.5,11.5,20,false),
(21,'Empedrado','Paraná','Corrientes',-27.95412,-58.81852,6.5,6.7,12.0,21,false),
(22,'Bella Vista','Paraná','Corrientes',-28.5184,-59.05932,6.0,6.4,13.0,22,false),
(23,'Goya','Paraná','Corrientes',-29.14377,-59.27304,5.2,5.7,14.0,23,false),
(24,'Reconquista','Paraná (brazo San Javier)','Santa Fe',-29.23756,-59.58116,5.1,5.3,14.5,24,false),
(25,'Esquina','Paraná','Corrientes',-30.01924,-59.53663,5.0,5.4,15.0,25,false),
(26,'La Paz','Paraná','Entre Ríos',-30.7342,-59.63812,5.8,6.15,16.0,26,false),
(27,'Santa Elena','Paraná','Entre Ríos',-30.95,-59.78333,6.9,7.25,17.0,27,false),
(28,'Hernandarias','Paraná','Entre Ríos',-31.22651,-59.99194,5.5,6.0,18.0,28,false),
(30,'Santa Fe','Paraná','Santa Fe',-31.65148,-60.70023,5.3,5.7,19.0,30,false),
(29,'Paraná','Paraná','Entre Ríos',-31.71824,-60.52257,4.7,5.0,19.5,29,false),
(31,'Diamante','Paraná','Entre Ríos',-32.07156,-60.64322,5.3,5.5,20.0,31,false),
(32,'Victoria','Paraná (delta)','Entre Ríos',-32.65585,-60.21958,4.6,5.9,21.0,32,false),
(33,'San Lorenzo / Pto. Gral. San Martín','Paraná','Santa Fe',-32.73363,-60.72567,5.2,5.7,22.0,33,false),
(34,'Rosario','Paraná','Santa Fe',-32.94327,-60.63082,5.0,5.25,23.0,34,false),
(35,'Villa Constitución','Paraná','Santa Fe',-33.23542,-60.3091,4.0,4.5,24.0,35,false),
(36,'San Nicolás','Paraná','Buenos Aires',-33.342,-60.18953,4.2,5.0,25.0,36,false),
(37,'Ramallo','Paraná','Buenos Aires',-33.47807,-59.9957,3.5,4.0,26.0,37,false),
(38,'San Pedro','Paraná','Buenos Aires',-33.67453,-59.64972,3.4,3.6,27.0,38,false),
(39,'Baradero','Paraná (Baradero)','Buenos Aires',-33.80069,-59.4956,2.7,3.1,28.0,39,false)
on conflict (id) do update set nombre=excluded.nombre, rio=excluded.rio, provincia=excluded.provincia, lat=excluded.lat, lon=excluded.lon,
  alerta_m=excluded.alerta_m, evacuacion_m=excluded.evacuacion_m, orden=excluded.orden, serie_ina=excluded.serie_ina, marea=excluded.marea;
