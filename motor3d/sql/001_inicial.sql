-- Base de datos propia del motor 3D (PostgreSQL). Las fotos y modelos NO van acá: van al almacenamiento S3 (SeaweedFS).
create extension if not exists pgcrypto;

create table if not exists trabajos (
  id              uuid primary key default gen_random_uuid(),
  restaurante_id  uuid not null,
  plato_id        uuid not null,
  usuario_id      uuid not null,
  estado          text not null default 'subiendo'
                  check (estado in ('subiendo','en_cola','procesando','revision','publicado','fallo','cancelado','descartado')),
  etapa           text not null default 'Esperando las fotos',
  etapa_num       int  not null default 0,
  etapas_total    int  not null default 13,
  progreso        int  not null default 0 check (progreso between 0 and 100),
  fotos_subidas   int  not null default 0,
  fotos_usadas    int,
  medidas_cargadas jsonb not null,              -- {ancho, prof, alto} en cm, las que puso el restaurante
  medidas_modelo  jsonb,                         -- las del modelo generado
  medidas_publicadas jsonb,                      -- las confirmadas al aprobar
  metricas        jsonb,
  puntaje         int,
  aprobable       boolean,
  motivos         jsonb,
  etapas          jsonb,                         -- tiempos reales de cada etapa
  error           text,
  error_tecnico   text,
  intentos        int  not null default 0,
  cancelar        boolean not null default false,
  tarea_celery    text,
  creado          timestamptz not null default now(),
  iniciado        timestamptz,
  terminado       timestamptz,
  publicado_en    timestamptz,
  actualizado     timestamptz not null default now()
);
create index if not exists trabajos_rest on trabajos (restaurante_id, creado desc);
create index if not exists trabajos_estado on trabajos (estado);

create table if not exists archivos (
  id          bigserial primary key,
  trabajo_id  uuid not null references trabajos(id) on delete cascade,
  tipo        text not null check (tipo in ('foto','cajas','maestro','movil','miniatura','registro')),
  clave       text not null unique,               -- clave en el almacenamiento S3
  bytes       bigint not null,
  sha256      text,
  creado      timestamptz not null default now(),
  borrado     timestamptz
);
create index if not exists archivos_trabajo on archivos (trabajo_id, tipo);

create table if not exists eventos (
  id          bigserial primary key,
  trabajo_id  uuid not null references trabajos(id) on delete cascade,
  momento     timestamptz not null default now(),
  nivel       text not null default 'info' check (nivel in ('info','aviso','error')),
  mensaje     text not null
);
create index if not exists eventos_trabajo on eventos (trabajo_id, id);

create or replace function tocar_actualizado() returns trigger language plpgsql as $$
begin new.actualizado := now(); return new; end $$;
drop trigger if exists trabajos_actualizado on trabajos;
create trigger trabajos_actualizado before update on trabajos for each row execute function tocar_actualizado();
