-- Database Webhook: events INSERT -> send-alert Edge Function -> FCM.
-- DECISIONS.md D-044. Mirrors shield-edge-function-prompt.md.
--
-- Why a webhook and not the RPi calling the function directly: the RPi would
-- then hold the Firebase admin private key. Pushing on INSERT means the RPi's
-- only job is to write one row, and the credential exists in exactly one place.
--
-- Requires pg_net. Supabase ships it; the extension is created here so the
-- chain is self-contained.
--
-- MANUAL STEP, run once, after this migration:
--
--   select shield_private.set_webhook_secret('<the 64 hex chars from .env WEBHOOK_SECRET>');
--
-- The value is never written to a file in this repo and never appears in a
-- migration. shield_private has no grants to anon or shield_rpi, so the secret
-- is unreadable through PostgREST by either client. Supabase Vault
-- (vault.create_secret) would work equally well and is the more conventional
-- choice; it was not used because it adds a second extension to depend on for
-- no gain at this size.

begin;

create extension if not exists pg_net with schema extensions;

-- ---------------------------------------------------------------------------
-- Private config, unreachable from the API surface.
-- ---------------------------------------------------------------------------
create schema if not exists shield_private;

revoke all on schema shield_private from public;
revoke all on schema shield_private from anon, shield_rpi;

create table if not exists shield_private.config (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);

-- Belt and braces: even if a future migration grants USAGE on the schema by
-- mistake, RLS with no policy means no rows are readable. Policies are granted
-- explicitly, never by default.
alter table shield_private.config enable row level security;

-- The only supported way in. Deliberately not SECURITY DEFINER on a callable
-- that takes the value from a client: this is the one function that must accept
-- an operator-supplied secret, and it is documented in tools/supabase_secrets.ps1.
create or replace function shield_private.set_webhook_secret(p_value text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    if p_value is null or length(p_value) < 32 then
        raise exception 'webhook secret must be at least 32 characters; got %',
            coalesce(length(p_value), 0);
    end if;

    insert into shield_private.config (key, value, updated_at)
    values ('webhook_secret', p_value, now())
    on conflict (key) do update
        set value = excluded.value,
            updated_at = excluded.updated_at;
end;
$$;

revoke all on function shield_private.set_webhook_secret(text) from public, anon, shield_rpi;

-- ---------------------------------------------------------------------------
-- The trigger.
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER because net.http_post and shield_private.config are not
-- readable by shield_rpi, and the trigger fires as the inserting role.
--
-- search_path = '' plus fully-qualified names: this function is reachable via
-- an INSERT, so a hijacked search_path would otherwise be a privilege
-- escalation. Nothing here may resolve a name against a caller-controlled schema.
create or replace function shield_private.notify_send_alert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_secret text;
begin
    select c.value into v_secret
      from shield_private.config c
     where c.key = 'webhook_secret';

    if v_secret is null then
        -- Raise rather than return quietly. A silent no-op here means events
        -- are stored and nobody is ever told: the exact failure this system
        -- exists to prevent, and the hardest kind to notice. alerts.py retries
        -- the insert on failure, so a raised exception is recoverable; a
        -- dropped notification is not.
        raise exception
            'shield_webhook_secret is not set; run: select shield_private.set_webhook_secret(''<value>'')';
    end if;

    -- net.http_post is queued, not synchronous: the INSERT does not wait on the
    -- network, so the RPi's reply-before-alert ordering (PROTOCOL.md 4.2) and
    -- the 3 s phone latency budget are unaffected by the function's runtime.
    perform net.http_post(
        url     := 'https://dnwwwsenujyaffcqiajt.supabase.co/functions/v1/send-alert',
        headers := jsonb_build_object(
            'Content-Type'      => 'application/json',
            'x-webhook-secret'  => v_secret
        ),
        body    := jsonb_build_object(
            'type'   => 'INSERT',
            'table'  => 'events',
            'record' => to_jsonb(new)
        )
    );

    return new;
end;
$$;

revoke all on function shield_private.notify_send_alert() from public, anon, shield_rpi;

drop trigger if exists events_insert_send_alert on public.events;
create trigger events_insert_send_alert
    after insert on public.events
    for each row execute function shield_private.notify_send_alert();

commit;