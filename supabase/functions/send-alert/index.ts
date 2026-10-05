// S.H.I.E.L.D `send-alert` Edge Function.
//
// Fires the FCM push whenever the RPi inserts an intruder row in `events`.
// DECISIONS.md D-044. Spec: shield-edge-function-prompt.md.
//
// Trigger path:
//
//   RPi -> INSERT public.events -> pg_net webhook -> this function -> FCM -> phone
//
// The Firebase admin private key lives in Supabase secrets and nowhere else.
// The RPi never sees it: its entire job on this path is to write one row.
//
// Zero imports. JWT signing is Web Crypto, not `jose`; the signed storage URL is
// a plain fetch against the Storage REST API, not supabase-js. That keeps the
// cold-start surface of a function that has to respond inside a 3 s budget as
// small as it can be.
//
// Environment (see tools/supabase_secrets.ps1):
//   FCM_SERVICE_ACCOUNT_JSON   compact service-account JSON, no real newlines
//   FCM_PROJECT_ID             e.g. shield-347f6
//   WEBHOOK_SECRET             64 hex chars, must match the pg_net trigger
// Injected by the Supabase runtime:
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

const FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging';
const OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token';
const FCM_SEND_URL = (projectId: string) =>
  `https://fcm.googleapis.com/v1/projects/${projectId}/messages:send`;

// PROTOCOL.md 3.3 caps a snapshot at 512 KB and the FCM payload carries a
// signed URL rather than bytes, so there is no message-size pressure here. The
// TTL is what bounds staleness: if the phone is unreachable for longer than
// this, a 60-second-old intruder alert is worse than none.
const MESSAGE_TTL_S = '60s';
const SIGNED_URL_TTL_S = 60 * 60;

// ---------------------------------------------------------------------------
// Access token cache. Module scope so a burst of events reuses one token
// instead of minting one per delivery.
// ---------------------------------------------------------------------------
let cachedAccessToken: { token: string; expiresAt: number } | null = null;

function b64url(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlJson(value: unknown): string {
  return b64url(new TextEncoder().encode(JSON.stringify(value)));
}

/**
 * Constant-time string comparison.
 *
 * `===` on a secret leaks its length and a matching prefix through timing,
 * which is the whole reason the sensor link uses HMAC (PROTOCOL.md 2.5). This
 * endpoint is internet-facing and the secret is a bearer value, so it gets the
 * same treatment rather than the same oversight.
 */
function timingSafeEqual(a: string, b: string): boolean {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  // Compare over the longer length so the loop count does not depend on the
  // shorter input, then fold in the length difference.
  const length = Math.max(a.length, b.length);
  let diff = a.length ^ b.length;
  for (let i = 0; i < length; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/** PEM (PKCS#8) to the DER bytes `crypto.subtle.importKey` expects. */
function pemToDer(pem: string): Uint8Array {
  if (pem.includes('BEGIN RSA PRIVATE KEY')) {
    throw new Error(
      'service account key is PKCS#1 (BEGIN RSA PRIVATE KEY); Firebase issues PKCS#8 (BEGIN PRIVATE KEY)',
    );
  }
  const base64 = pem
    .replace(/-----BEGIN [^-]+-----/, '')
    .replace(/-----END [^-]+-----/, '')
    .replace(/\s+/g, '');
  const binary = atob(base64);
  const der = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) der[i] = binary.charCodeAt(i);
  return der;
}

async function getAccessToken(account: {
  client_email: string;
  private_key: string;
}): Promise<string> {
  // 60s of margin: a token that expires mid-flight turns into a 401 whose
  // cause is not obvious from the logs.
  const nowSeconds = Math.floor(Date.now() / 1000);
  if (cachedAccessToken && cachedAccessToken.expiresAt - 60 > nowSeconds) {
    return cachedAccessToken.token;
  }

  const header = b64urlJson({ alg: 'RS256', typ: 'JWT' });
  const claims = b64urlJson({
    iss: account.client_email,
    scope: FCM_SCOPE,
    aud: OAUTH_TOKEN_URL,
    iat: nowSeconds,
    exp: nowSeconds + 3600,
  });
  const signingInput = `${header}.${claims}`;

  const key = await crypto.subtle.importKey(
    'pkcs8',
    pemToDer(account.private_key),
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(signingInput)),
  );

  const response = await fetch(OAUTH_TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: `${signingInput}.${b64url(signature)}`,
    }),
  });

  if (!response.ok) {
    throw new Error(`oauth2 token exchange failed: ${response.status} ${await response.text()}`);
  }

  const payload = await response.json();
  cachedAccessToken = {
    token: payload.access_token,
    expiresAt: nowSeconds + Number(payload.expires_in ?? 3600),
  };
  return cachedAccessToken.token;
}

// ---------------------------------------------------------------------------
// Supabase REST helpers
// ---------------------------------------------------------------------------
function supabaseHeaders(): Record<string, string> {
  const key = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? '';
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    'Content-Type': 'application/json',
  };
}

function supabaseUrl(path: string): string {
  return `${Deno.env.get('SUPABASE_URL')}${path}`;
}

/**
 * Mint a signed URL for the snapshot.
 *
 * This is what `supabase-js` does internally: POST to the Storage sign
 * endpoint with the service key and splice the returned path onto
 * SUPABASE_URL/storage/v1. Doing it with fetch avoids pulling in the SDK.
 */
async function signedSnapshotUrl(imagePath: string): Promise<string> {
  const response = await fetch(
    supabaseUrl(`/storage/v1/object/sign/snapshots/${imagePath.split('/').map(encodeURIComponent).join('/')}`),
    {
      method: 'POST',
      headers: supabaseHeaders(),
      body: JSON.stringify({ expiresIn: SIGNED_URL_TTL_S }),
    },
  );
  if (!response.ok) {
    throw new Error(`signed URL failed for ${imagePath}: ${response.status} ${await response.text()}`);
  }
  const { signedURL } = await response.json();
  return `${Deno.env.get('SUPABASE_URL')}/storage/v1${signedURL}`;
}

async function allDeviceTokens(): Promise<Array<{ id: string; fcm_token: string }>> {
  const response = await fetch(
    supabaseUrl('/rest/v1/devices?select=id,fcm_token&order=created_at.asc'),
    { headers: supabaseHeaders() },
  );
  if (!response.ok) {
    throw new Error(`device lookup failed: ${response.status} ${await response.text()}`);
  }
  return await response.json();
}

async function deleteDevice(id: string): Promise<void> {
  await fetch(supabaseUrl(`/rest/v1/devices?id=eq.${encodeURIComponent(id)}`), {
    method: 'DELETE',
    headers: supabaseHeaders(),
  });
}

/**
 * Claim the event for delivery.
 *
 * Atomic, and it is the whole reason `events.push_sent_at` exists. pg_net
 * retries a timed-out webhook, and a 200 that was actually a dropped response
 * is indistinguishable from one that succeeded. Without a claim the retry
 * rings the phone a second time for a single intruder, and an owner whose phone
 * cries wolf once for one event stops trusting it.
 *
 * Returns false when someone already claimed it, which means this delivery is a
 * duplicate and must not push.
 */
async function claimEvent(eventId: string): Promise<boolean> {
  const response = await fetch(
    supabaseUrl(`/rest/v1/events?id=eq.${encodeURIComponent(eventId)}&push_sent_at=is.null`),
    {
      method: 'PATCH',
      headers: { ...supabaseHeaders(), Prefer: 'return=representation' },
      body: JSON.stringify({ push_sent_at: new Date().toISOString() }),
    },
  );
  if (!response.ok) {
    throw new Error(`claim failed for ${eventId}: ${response.status} ${await response.text()}`);
  }
  const rows = await response.json();
  return Array.isArray(rows) && rows.length > 0;
}

// ---------------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------------
interface EventRow {
  id: string;
  ts: number | string;
  status: number;
  confidence: number;
  image_path: string;
  needs_verdict?: boolean;
}

/** FCM data values are strings; the app parses them back. */
function toFcmData(row: EventRow, imageUrl: string): Record<string, string> {
  return {
    event_id: String(row.id),
    ts: String(row.ts),
    status: String(row.status),
    confidence: String(row.confidence),
    image_url: imageUrl,
    // Only status 2 ever reaches here (see the skip below), so a verdict is
    // always being asked for. Echoed explicitly because the app routes on it.
    needs_verdict: String(row.needs_verdict ?? true),
  };
}

async function sendToDevice(
  accessToken: string,
  projectId: string,
  token: string,
  data: Record<string, string>,
): Promise<{ ok: boolean; dead: boolean; error: string }> {
  const response = await fetch(FCM_SEND_URL(projectId), {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: {
        token,
        data,
        android: {
          priority: 'HIGH',
          ttl: MESSAGE_TTL_S,
        },
      },
    }),
  });

  if (response.ok) return { ok: true, dead: false, error: '' };

  const body = await response.text();
  // UNREGISTERED means the token was invalidated (app uninstalled, data
  // cleared); NOT_FOUND means FCM has no record of it. Both are permanent, so
  // the row is deleted rather than retried -- shield-edge-function-prompt.md 7.
  const dead = response.status === 404 || /UNREGISTERED|NOT_FOUND/.test(body);
  return { ok: false, dead, error: `${response.status} ${body}` };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

Deno.serve(async (request: Request): Promise<Response> => {
  // Supabase runtime health probe. No auth: it reveals nothing and returning
  // 401 here makes the platform mark the function unhealthy.
  if (request.method === 'GET' || request.method === 'OPTIONS') {
    return json({ status: 'ok', function: 'send-alert' });
  }

  const secret = Deno.env.get('WEBHOOK_SECRET');
  const provided = request.headers.get('x-webhook-secret');
  if (!secret || !provided || !timingSafeEqual(secret, provided)) {
    return json({ error: 'unauthorized' }, 401);
  }

  const projectId = Deno.env.get('FCM_PROJECT_ID');
  const accountRaw = Deno.env.get('FCM_SERVICE_ACCOUNT_JSON');
  if (!projectId || !accountRaw) {
    return json({ error: 'FCM_PROJECT_ID / FCM_SERVICE_ACCOUNT_JSON not set' }, 500);
  }

  let payload: { record?: EventRow } & Partial<EventRow>;
  try {
    payload = await request.json();
  } catch {
    return json({ error: 'body is not JSON' }, 400);
  }

  // Accepts both the pg_net envelope this deployment sends
  // ({type, table, record}) and a bare row, so the trigger can be changed to
  // PostgREST's native webhook shape without editing the handler.
  const row = payload.record ?? (payload as unknown as EventRow);
  if (!row || typeof row.id !== 'string' || typeof row.image_path !== 'string') {
    return json({ error: 'body has no usable event row' }, 400);
  }

  // PROTOCOL.md 2.4.3: status 0 is clear and 1 is a known face. Neither is an
  // event the user needs to be woken for.
  if (row.status !== 2) {
    return json({ sent: 0, failed: 0, skipped: true, reason: `status ${row.status}` });
  }

  let claimed: boolean;
  try {
    claimed = await claimEvent(row.id);
  } catch (error) {
    // Do not claim on failure: returning 500 here makes pg_net retry, which is
    // the correct outcome. Silently returning 200 would swallow the alert.
    return json({ error: String(error) }, 500);
  }
  if (!claimed) {
    return json({ sent: 0, failed: 0, skipped: true, reason: 'already delivered' });
  }

  let accessToken: string;
  try {
    accessToken = await getAccessToken(JSON.parse(accountRaw));
  } catch (error) {
    return json({ error: String(error) }, 500);
  }

  let imageUrl: string;
  // No snapshot is attached when the trigger produced no usable face: the RPi
  // still alarms (absence of a face is not absence of an intruder) but publishes
  // an empty image_path. Signing "" would be a guaranteed 400 on every such
  // alert, so skip it rather than spending a round trip to learn that.
  if (!row.image_path) {
    imageUrl = '';
  } else {
    try {
      imageUrl = await signedSnapshotUrl(row.image_path);
    } catch (error) {
      // The event is claimed but unsent. Prefer a snapshot-less alert over no
      // alert: the user can still see the time and confidence and ask the RPi for
      // more, whereas silence leaves them believing the house is quiet.
      console.error('signed URL failed, sending without image_url:', String(error));
      imageUrl = '';
    }
  }

  let devices: Array<{ id: string; fcm_token: string }>;
  try {
    devices = await allDeviceTokens();
  } catch (error) {
    return json({ error: String(error) }, 500);
  }

  const data = toFcmData(row, imageUrl);
  const failures: Array<{ device: string; error: string }> = [];
  let sent = 0;

  for (const device of devices) {
    const result = await sendToDevice(accessToken, projectId, device.fcm_token, data);
    if (result.ok) {
      sent += 1;
      continue;
    }
    failures.push({ device: device.id, error: result.error });
    console.error(`FCM send failed for device ${device.id}:`, result.error);
    if (result.dead) {
      await deleteDevice(device.id);
    }
  }

  // Always 200. The alert is stored and claimed either way; a non-200 would
  // trigger a pg_net retry that claimEvent would then suppress, adding latency
  // and log noise without delivering anything extra.
  return json({ sent, failed: failures.length, failures });
});