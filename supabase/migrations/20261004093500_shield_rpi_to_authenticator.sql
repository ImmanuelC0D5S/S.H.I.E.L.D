-- Grant shield_rpi to authenticator, so PostgREST can assume it.
--
-- Found 2026-10-04, while verifying the first minted SUPABASE_RPI_TOKEN against
-- the live project. Every request carrying a valid shield_rpi JWT came back:
--
--   HTTP 403   {"code":"42501","message":"permission denied to set role \"shield_rpi\""}
--
-- Nothing in 20261003090100 is visibly wrong: the role exists, nologin, and its
-- grants and RLS policies are all exactly as specified. The missing statement is
-- the one that is not there. That migration creates the role and grants
-- privileges *to* it, but never grants it *to authenticator*.
--
-- Why that is fatal and not cosmetic: Supabase's PostgREST connects as
-- `authenticator` and issues `set role <role claim>` on every request. The three
-- roles the platform ships -- anon, authenticated, service_role -- are all
-- members of `authenticator` already. A role created by hand is not. So the
-- RPi's token validates as a JWT, carries a legitimate role claim, and is then
-- refused at the only step that matters. The failure surfaces as a 403 on the
-- first insert, which reads like a permissions problem rather than a missing
-- membership, and it blocks the entire alert path at the door.
--
-- This is membership, not a grant of privileges. shield_rpi still holds exactly
-- what 20261003090100 gave it -- insert events, select them, update
-- (verdict, verdict_at), upload to 'snapshots', and nothing else. RLS still
-- applies, because the role is not service_role and service_role is the only
-- thing here that bypasses it (DECISIONS.md D-040). Granting authenticator the
-- ability to *become* shield_rpi does not let it *be* shield_rpi: the SET ROLE
-- still only happens for a request whose signed JWT carries role=shield_rpi.
--
-- Idempotent, so re-running it on a project that already has the membership is
-- a no-op rather than an error.

begin;

grant shield_rpi to authenticator;

commit;
