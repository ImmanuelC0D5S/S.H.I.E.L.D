-- S.H.I.E.L.D core schema. DECISIONS.md D-040 (Supabase replaces FastAPI).
--
-- Column shapes are fixed by shield-app-prompt.md and shield-edge-function-prompt.md.
-- Three columns are deliberate additions; each is called out at its definition.
--
-- PROTOCOL.md section 2 (the ESP32 <-> RPi link) is untouched by this file.

begin;

-- ---------------------------------------------------------------------------
-- devices - one row per registered phone, holding its FCM registration token.
-- ---------------------------------------------------------------------------
create table if not exists public.devices (
    id           uuid        primary key default gen_random_uuid(),
    -- The send-alert function pushes to this. Unique so the app's upsert on
    -- every launch and on every onNewToken collapses to one row instead of
    -- accumulating a stale row per reinstall (shield-app-prompt.md P0 #5).
    fcm_token    text        not null unique,
    platform     text        not null default 'android',
    app_version  text,
    created_at   timestamptz not null default now(),
    -- ADDITION. Lets a dead token be aged out rather than retried forever. The
    -- function deletes a token on UNREGISTERED/NOT_FOUND regardless; this is
    -- the manual-triage signal when pushes stop arriving.
    last_seen_at timestamptz not null default now()
);

comment on column public.devices.last_seen_at is
    'Last successful token registration. Diagnostic only; push failures delete the row.';

-- ---------------------------------------------------------------------------
-- events - one row per intrusion detection, from trigger to verdict.
-- ---------------------------------------------------------------------------
create table if not exists public.events (
    -- Client-generated UUID4 (shield-rpi-prompt.md). Client-generated is what
    -- makes the RPi's retries idempotent: a repeat POST re-uses the id and
    -- hits the primary key instead of creating a second event.
    id          uuid primary key,

    -- Unix seconds, UTC, integer. Deliberately bigint rather than timestamptz:
    -- this is the exact value carried in the FCM payload ("ts"), so storing it
    -- as-is removes a class of off-by-one-second and timezone bugs between the
    -- RPi, the database and the phone.
    ts          bigint      not null,

    -- 0 clear, 1 known, 2 unknown/unusable. PROTOCOL.md section 2.4.3.
    -- The function pushes only on 2.
    status      smallint    not null check (status in (0, 1, 2)),

    -- Detector confidence 0-100.
    confidence  smallint    not null check (confidence between 0 and 100),

    -- Object path within the private 'snapshots' bucket, never a public URL.
    -- A signed URL is minted per delivery so a leaked link expires in 60
    -- minutes instead of granting permanent access to footage of a person's home.
    image_path  text        not null,

    -- ADDITION. The FCM payload carries needs_verdict and the function gates on
    -- it, but the prompt only specified it as a payload field. Persisting it
    -- keeps the payload derived from the row rather than re-derived from the
    -- request.
    needs_verdict boolean   not null default true,

    device_id   text,
    identity    text,
    trigger_tier text       check (trigger_tier in ('high', 'low')),
    latency_ms  integer,
    person_detected boolean,
    face_detected   boolean,

    -- null until the user taps Known or Intruder. The RPi polls this and stops
    -- when it is non-null (PROTOCOL.md section 3.7 stop condition).
    verdict     text        check (verdict in ('known', 'intruder')),
    verdict_at  bigint,

    -- ADDITION. Set by the send-alert function as an atomic claim before it
    -- pushes. Without it, a webhook retry or a function timeout re-delivers the
    -- same event and rings the phone a second time for one intrusion. A phone
    -- that alarms twice for one intruder trains its owner to ignore it.
    push_sent_at timestamptz,

    created_at  timestamptz not null default now()
);

comment on column public.events.push_sent_at is
    'Atomic idempotency claim for the send-alert function. Set before the FCM POST; a second webhook delivery finds it already set and returns without pushing.';

-- Newest first is the only access pattern the app has (event log, newest at top).
create index if not exists events_created_at_idx on public.events (created_at desc);

-- The RPi's verdict poll is "select ... where id = ? and verdict is null",
-- filtered on unresolved rows. A partial index keeps that off a full scan
-- instead of indexing every resolved event forever.
create index if not exists events_unresolved_idx
    on public.events (ts)
    where verdict is null;

-- ---------------------------------------------------------------------------
-- system_state - arm/disarm, owned by the app (shield-app-prompt.md P0 #5).
-- Single row, enforced.
-- ---------------------------------------------------------------------------
create table if not exists public.system_state (
    id         smallint    primary key default 1 check (id = 1),
    armed      boolean     not null default true,
    updated_at timestamptz not null default now(),
    -- Who changed it last. Useful when a Pi keeps alerting after the phone
    -- says it disarmed: this distinguishes "the app never wrote the flag" from
    -- "something else set it back".
    updated_by text
);

comment on table public.system_state is
    'Single-row table (id is pinned to 1) holding the arm flag. The RPi reads it and replies status 0 while disarmed.';

insert into public.system_state (id, armed) values (1, true)
on conflict (id) do nothing;

create or replace function public.system_state_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists system_state_touch_trg on public.system_state;
create trigger system_state_touch_trg
    before update on public.system_state
    for each row execute function public.system_state_touch();

-- ---------------------------------------------------------------------------
-- Storage: private 'snapshots' bucket.
-- ---------------------------------------------------------------------------
-- public = false is the whole point. public = true would make every snapshot
-- readable by anyone who can guess or obtain the path, with no expiry.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('snapshots', 'snapshots', false, 5242880, array['image/jpeg'])
on conflict (id) do nothing;

-- PROTOCOL.md section 3.3 caps snapshots at 512 KB. The bucket allows 5 MB so a
-- misbehaving RPi cannot fail the insert outright, but it still cannot fill the
-- project; alerts.py enforces the 512 KB contract before uploading.

commit;