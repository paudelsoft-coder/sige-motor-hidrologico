-- Motor hidrológico SIGE · ajuste: la tabla de represas acepta Itaipú y las represas del Iguazú (datos del ONS)
alter table public.sige_hidro_represas drop constraint if exists sige_hidro_represas_represa_check;
select conname from pg_constraint where conrelid = 'public.sige_hidro_represas'::regclass;
