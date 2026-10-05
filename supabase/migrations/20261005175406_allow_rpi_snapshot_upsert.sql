-- Let shield_rpi upsert and read snapshots in the private bucket. DECISIONS.md D-052.
--
-- RECONSTITUTED 2026-10-06 from the live database, not from the original ad-hoc run.
--
-- Like the companion grant, these two policies were applied to project
-- dnwwwsenujyaffcqiajt through apply_migration on 2026-10-05 and were never
-- written to a file, so `supabase db push` could not reproduce them. A fresh
-- project would have had neither policy, and every snapshot upload would have
-- failed 403 -- which is the D-052 failure mode exactly, reintroduced by the
-- absence of this file.
--
-- Why they are needed. alerts.py::upload_snapshot sends `x-upsert: true`, which
-- makes the Storage API perform an UPSERT rather than a plain INSERT. An upsert is
-- INSERT ... ON CONFLICT DO UPDATE, and RLS evaluates the UPDATE branch too. So
-- 20261003090100's snapshots_insert_rpi was necessary but not sufficient: the
-- upload passed the INSERT check and then failed the UPDATE check.
--
-- This was isolated rather than assumed before the policies were added: a plain
-- POST to the same path returned 200 while the same request with x-upsert
-- returned 403, which proved the grant and the INSERT policy were already correct
-- and pointed at the missing UPDATE branch specifically.
--
-- Scoped to bucket_id = 'snapshots' so the RPi still cannot touch any other
-- bucket, and still no DELETE policy: the RPi must not be able to erase the
-- evidence of an intrusion it recorded.
--
-- Nothing in the test suite pins this policy: rpi/tests/test_credentials.py
-- asserts on the RPi side and has no Postgres visibility. Dropping it fails every
-- upload at runtime, not at build time -- which is precisely why it went missing
-- once already without anything noticing.

begin;

drop policy if exists snapshots_update_rpi on storage.objects;
create policy snapshots_update_rpi on storage.objects
    for update to shield_rpi
    using (bucket_id = 'snapshots')
    with check (bucket_id = 'snapshots');

drop policy if exists snapshots_select_rpi on storage.objects;
create policy snapshots_select_rpi on storage.objects
    for select to shield_rpi
    using (bucket_id = 'snapshots');

commit;