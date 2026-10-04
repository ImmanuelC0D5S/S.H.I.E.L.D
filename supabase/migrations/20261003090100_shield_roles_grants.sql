-- Roles, grants and RLS. DECISIONS.md D-040.
--
-- Two distinct clients talk to this database and must not be able to do each
-- other's jobs:
--
--   anon        the Android app. Authenticated by SUPABASE_KEY
--               (sb_publishable_..., a project-supplied credential that ships in
--               the APK and is therefore not a secret). Scoped to exactly what
--               shield-app-prompt.md P0 #5 needs.
--
--   shield_rpi  the compute node. NOT the service-role key -- a service-role
--               bypasses RLS entirely, which would make every policy below
--               decorative. The RPi authenticates as this role with a JWT whose
--   'role' claim is shield_rpi, minted by tools/mint_role_token.py.
--
-- Neither role can delete an event. Events are the audit trail of who was in
-- someone's house; the operator being able to erase that is a worse property
-- than the inconvenience of a full table.

begin;

-- ---------------------------------------------------------------------------
-- shield_rpi
-- ---------------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'shield_rpi') then
        create role shield_rpi nologin;
    end if;
end;
$$;

grant usage on schema public to shield_rpi;

-- Insert events (the RPi is the only writer), read them back for the verdict
-- poll, and update the verdict when the user answers on the phone.
grant select, insert on public.events to shield_rpi;
grant update (verdict, verdict_at) on public.events to shield_rpi;

grant select on public.system_state to shield_rpi;

-- The RPi never registers a device and never deletes anything.
grant usage on schema storage to shield_rpi;
grant insert on storage.objects to shield_rpi;
grant select on storage.objects to shield_rpi;

revoke delete on public.events from shield_rpi;
revoke update on public.events from shield_rpi;
grant update (verdict, verdict_at) on public.events to shield_rpi;

-- ---------------------------------------------------------------------------
-- anon (the app)
-- ---------------------------------------------------------------------------
-- Start from nothing on these tables so the grants below are the complete list.
revoke all on public.events from anon;
revoke all on public.system_state from anon;
revoke all on public.devices from anon;

grant usage on schema public to anon;

-- Read the event log. Needs SELECT because the app renders the list and
-- because the "update verdict" response is read-modify-write on one row.
grant select on public.events to anon;

-- P0 #3 / P0 #4: Known / Intruder writes events.verdict and nothing else.
-- Column-level UPDATE, not table-level. With a table grant the app could also
-- rewrite 'confidence' or 'status' on a historical event and falsify the log.
grant update (verdict, verdict_at) on public.events to anon;

-- P0 #5: register the FCM token on first launch and in onNewToken.
grant select, insert, update on public.devices to anon;

-- P0 #5: arm/disarm toggle.
grant select on public.system_state to anon;
grant update (armed, updated_by) on public.system_state to anon;

revoke insert, delete on public.system_state from anon;
revoke insert on public.events from anon;
revoke delete on public.events from anon;

-- ---------------------------------------------------------------------------
-- Row level security
-- ---------------------------------------------------------------------------
-- Grants decide which columns a role may touch; RLS decides which rows. Both
-- are required: with grants alone, anon could write a verdict onto any row and
-- with policies alone it could write any column.
alter table public.events enable row level security;
alter table public.system_state enable row level security;
alter table public.devices enable row level security;

-- events ---------------------------------------------------------------------
drop policy if exists events_select_app on public.events;
create policy events_select_app on public.events
    for select to anon
    using (true);

drop policy if exists events_update_verdict_app on public.events;
create policy events_update_verdict_app on public.events
    for update to anon
    using (true)
    with check (true);

drop policy if exists events_select_rpi on public.events;
create policy events_select_rpi on public.events
    for select to shield_rpi
    using (true);

drop policy if exists events_insert_rpi on public.events;
create policy events_insert_rpi on public.events
    for insert to shield_rpi
    with check (true);

-- The RPi's verdict write is a stand-down/escalate signal, not new evidence.
drop policy if exists events_update_verdict_rpi on public.events;
create policy events_update_verdict_rpi on public.events
    for update to shield_rpi
    using (true)
    with check (true);

-- system_state ---------------------------------------------------------------
-- Single-row table. using (id = 1) rather than true, so a future second row
-- cannot appear outside the app's reach by accident.
drop policy if exists system_state_select_app on public.system_state;
create policy system_state_select_app on public.system_state
    for select to anon
    using (id = 1);

drop policy if exists system_state_update_app on public.system_state;
create policy system_state_update_app on public.system_state
    for update to anon
    using (id = 1)
    with check (id = 1);

drop policy if exists system_state_select_rpi on public.system_state;
create policy system_state_select_rpi on public.system_state
    for select to shield_rpi
    using (true);

-- devices --------------------------------------------------------------------
-- KNOWN LIMITATION, recorded rather than papered over: the app has no Supabase
-- Auth session, so anon cannot prove which install it is. These policies
-- therefore permit an app client to update any devices row, not only its own.
-- That is acceptable only because this is a single-operator deployment with one
-- phone. If a second operator ever enrolls, this must become
--   using ((select auth.uid()) = owner)
-- which requires real sign-in. See DECISIONS.md D-040.
drop policy if exists devices_all_app on public.devices;
create policy devices_all_app on public.devices
    for all to anon
    using (true)
    with check (true);

-- storage.objects ------------------------------------------------------------
-- The RPi uploads snapshots. It writes only, only into 'snapshots'.
-- Deliberately no select policy for shield_rpi: reading snapshots back is not
-- something the RPi needs, and the row it just inserted is already known to it.
drop policy if exists snapshots_insert_rpi on storage.objects;
create policy snapshots_insert_rpi on storage.objects
    for insert to shield_rpi
    with check (bucket_id = 'snapshots');

commit;