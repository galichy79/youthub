// PO Token generator — no browser, pure Node + jsdom + bgutils-js.
//
// Replaces the Camoufox / Playwright bootstrap. Runs YouTube's BotGuard
// JavaScript in a jsdom VM, fetches a WAA integrity token, and mints
// PO Tokens on demand.
//
// API:
//   await getPoToken(visitorData)
//     → string  (websafe-base64 PO Token bound to visitorData)
//
// First call:  ~3-5s (challenge fetch + BotGuard VM init + integrity fetch).
// Subsequent calls reuse the cached integrity token and just mint a new
// token (~50ms). The cache is invalidated when the token's TTL expires.
//
// Why visitor-bound (not video-bound) tokens: YouTube's WEB SABR
// streaming accepts a visitor-bound PO Token for any video in the same
// session, so one token per visitorData covers many videos.

import { JSDOM } from 'jsdom';
import { BG } from 'bgutils-js';
import { Innertube } from 'youtubei.js';

// Standard YouTube WAA "create challenge" request key. Same key used
// by yt-dlp's bgutil-pot plugin and youtubei.js — empirically stable.
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';

function log(msg) { process.stderr.write(`[po_token] ${msg}\n`); }

// Cached session state — one BotGuard VM + integrity token covers many
// videos until the integrity token approaches expiry.
let cached = null;
//   {
//     visitorData,
//     integrityTokenData,
//     webPoSignalOutput,
//     expiresAt,     // ms epoch
//   }

async function _fetchChallengeViaInnertube() {
  // Per bgutils README, the YT InnerTube /att/get endpoint returns a
  // challenge structure that's already wired for WEB-client SABR use.
  // We tried the direct WAA /Create call first and the snapshot's
  // webPoSignalOutput[0] callback returned a non-function ("APF:Failed").
  // The InnerTube-fetched challenge avoids that.
  const inn = await Innertube.create({ generate_session_locally: true });
  const ch = await inn.getAttestationChallenge('ENGAGEMENT_TYPE_UNBOUND');
  if (!ch?.bg_challenge) throw new Error('Innertube returned no bg_challenge');
  return ch.bg_challenge;
}


async function _runChallenge(visitorData) {
  // jsdom gives us document, navigator, requestAnimationFrame etc. that
  // BotGuard's interpreter expects. Pretending to be a real browser at
  // youtube.com matters: BotGuard inspects `location`, `document.cookie`,
  // and similar surfaces during the snapshot.
  // The default jsdom UA self-identifies as `jsdom/X.Y` — BotGuard
  // checks navigator.userAgent and silently refuses to wire up the
  // mint callback when it sees that. Override to a plausible Chrome.
  const realUA =
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    + '(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36';
  const dom = new JSDOM(
    '<!DOCTYPE html><html><head></head><body></body></html>',
    {
      url: 'https://www.youtube.com/',
      referrer: 'https://www.youtube.com/',
      userAgent: realUA,
      pretendToBeVisual: true,
      runScripts: 'outside-only',
    },
  );
  const w = dom.window;
  w.self = w;
  w.globalThis = w;

  log('fetching challenge via InnerTube…');
  const bgChallenge = await _fetchChallengeViaInnertube();
  const interpreterUrl = bgChallenge.interpreter_url
    .private_do_not_access_or_else_trusted_resource_url_wrapped_value;
  const program = bgChallenge.program;
  const globalName = bgChallenge.global_name;
  log(`challenge ok (globalName=${globalName})`);

  log(`fetching interpreter from ${interpreterUrl.slice(0, 80)}…`);
  const scriptRes = await fetch(
    interpreterUrl.startsWith('//')
      ? `https:${interpreterUrl}`
      : interpreterUrl);
  if (!scriptRes.ok) throw new Error(`interpreter HTTP ${scriptRes.status}`);
  const interpreterSrc = await scriptRes.text();

  // Execute the interpreter inside the jsdom window. jsdom disables
  // direct eval; Function constructor bound to the jsdom global works.
  log(`evaluating interpreter (${interpreterSrc.length}B)`);
  new w.Function(interpreterSrc)();
  if (!w[globalName]) {
    throw new Error(`interpreter did not define ${globalName}`);
  }

  // Snapshot the VM to produce the BotGuard response that proves we ran
  // the program. Also collect webPoSignalOutput — the closures used by
  // WebPoMinter to mint future tokens with the same integrity proof.
  log('creating BotGuard client + snapshotting…');
  const botguard = await BG.BotGuardClient.create({
    program: program,
    globalName: globalName,
    globalObj: w,
  });
  const webPoSignalOutput = [];
  const botguardResponse = await botguard.snapshot({ webPoSignalOutput });
  log(`snapshot done (${botguardResponse.length}B, `
      + `signal=${typeof webPoSignalOutput[0]})`);

  // Exchange the snapshot for an integrity token (good for ~1 hour).
  log('fetching integrity token…');
  const itRes = await fetch(
    'https://jnn-pa.googleapis.com/$rpc/google.internal.waa.v1.Waa/GenerateIT',
    {
      method: 'POST',
      headers: {
        'content-type': 'application/json+protobuf',
        'x-goog-api-key': 'AIzaSyDyT5W0Jh49F30Pqqtyfdf7pDLFKLJoAnw',
        'x-user-agent': 'grpc-web-javascript/0.1',
        'user-agent':
          'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
          + 'AppleWebKit/537.36 (KHTML, like Gecko)',
      },
      body: JSON.stringify([REQUEST_KEY, botguardResponse]),
    },
  );
  if (!itRes.ok) throw new Error(`integrity HTTP ${itRes.status}`);
  const itJson = await itRes.json();
  const [
    integrityToken, estimatedTtlSecs,
    mintRefreshThreshold, websafeFallbackToken,
  ] = itJson;
  if (!integrityToken)
    throw new Error('GenerateIT returned no integrityToken');
  log(`integrity token ok (ttl=${estimatedTtlSecs}s)`);

  cached = {
    visitorData,
    integrityTokenData: {
      integrityToken,
      estimatedTtlSecs,
      mintRefreshThreshold,
      websafeFallbackToken,
    },
    webPoSignalOutput,
    // Refresh at 80% of TTL to avoid races at exactly the expiry edge.
    expiresAt: Date.now() + (estimatedTtlSecs ?? 3600) * 800,
  };
  return cached;
}

export async function getPoToken(visitorData, contentBinding) {
  // `visitorData` is used to scope/cache the BotGuard session (one
  // integrity token per visitor identity, reused).
  // `contentBinding` is what the resulting PO Token will be bound to —
  // pass the videoId for SABR /videoplayback content-bound tokens.
  // If omitted, the token is bound to visitorData (session-bound).
  if (!visitorData) throw new Error('visitorData is required');
  const identifier = contentBinding || visitorData;
  const valid = cached
    && cached.visitorData === visitorData
    && Date.now() < cached.expiresAt;
  if (!valid) {
    await _runChallenge(visitorData);
  }
  // Manual minting. bgutils-js's WebPoMinter.create has an
  // `mintCallback instanceof Function` check that fails for callbacks
  // created inside jsdom — cross-realm Functions don't satisfy the
  // node-side `instanceof Function`. Using typeof works in both.
  //
  // The minter is derived ONCE per integrity token and cached. Deriving
  // it again for every mint (`webPoSignalOutput[0](itBytes)`) makes each
  // successive token ~88 bytes longer — the snapshot's signal-output
  // closure accumulates state — so a re-minted token would not match the
  // shape of the original. Reusing one mintFn keeps tokens a stable size,
  // which is what makes mid-session re-attestation (see sabr_bridge.mjs)
  // viable: mint once at start, mint again on demand, same token size.
  if (typeof cached.mintFn !== 'function') {
    const getMinter = cached.webPoSignalOutput[0];
    if (typeof getMinter !== 'function')
      throw new Error('webPoSignalOutput[0] is not a function');
    const itBytes = _b64urlDecode(cached.integrityTokenData.integrityToken);
    cached.mintFn = await getMinter(itBytes);
    if (typeof cached.mintFn !== 'function')
      throw new Error('mint callback is not a function');
  }
  const out = await cached.mintFn(new TextEncoder().encode(identifier));
  if (!(out && out.length))
    throw new Error('mint returned empty');
  return _b64urlEncode(out);
}


function _b64urlDecode(s) {
  // input is base64url; convert to standard base64 and decode
  const std = s.replace(/-/g, '+').replace(/_/g, '/').replace(/\./g, '=');
  const bin = atob(std);
  return Uint8Array.from(bin, c => c.charCodeAt(0));
}


function _b64urlEncode(u8) {
  const bin = String.fromCharCode(...u8);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_');
}

export function clearPoTokenCache() {
  cached = null;
}

// CLI: node po_token.mjs <visitorData> [contentBinding]
//   smoke-test mode if only visitorData given — logs token info to stderr
//   mint mode if both given — prints ONLY the token to stdout (for callers)
if (import.meta.url === `file://${process.argv[1]}`) {
  const vd = process.argv[2];
  const cb = process.argv[3];
  if (!vd) {
    console.error('usage: node po_token.mjs <visitorData> [contentBinding]');
    process.exit(2);
  }
  (async () => {
    if (cb) {
      // Mint mode — silent, only the token to stdout.
      const tok = await getPoToken(vd, cb);
      process.stdout.write(tok);
    } else {
      const t0 = Date.now();
      const tok = await getPoToken(vd);
      log(`token: ${tok.slice(0, 40)}…  (${tok.length}B)  in ${Date.now()-t0}ms`);
      const tok2 = await getPoToken(vd);
      log(`token: ${tok2.slice(0, 40)}…  (${tok2.length}B)  in ${Date.now()-t0}ms (cached)`);
    }
  })().catch(e => { console.error(e); process.exit(1); });
}
