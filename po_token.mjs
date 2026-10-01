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

// Homepage URL and the UA used both to fetch it and to fake the browser
// inside jsdom: the page context and the snapshot must come from the
// same identity, or the server does not match them up.
const HOMEPAGE = 'https://www.youtube.com/';
const REAL_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36';

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


function _decodeEscapes(s) {
  // The homepage carries its player config inside a JS string literal, so
  // quotes and braces arrive as \x22 / \x7b and '&' as &.
  return s
    .replace(/\\x([0-9A-Fa-f]{2})/g,
             (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/\\u([0-9A-Fa-f]{4})/g,
             (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/\\\//g, '/');
}

function _field(text, name, valuePrefix = '') {
  const m = text.match(new RegExp(`"${name}":"(${valuePrefix}[^"]+)"`));
  return m ? m[1] : null;
}

// Take every `bgChallenge` object out of the decoded page by matching
// braces, instead of slicing a fixed window around the key.
//
// A fixed window is a race against how large YouTube's current BotGuard
// program happens to be, and that race is lost regularly. Measured
// 2026-10-01: `program` ran 38 695-39 111 characters on most responses,
// fitting the old 40 000-character forward window by only ~900
// characters, but 40 371 and 42 899 on the responses that broke
// playback. A cut value has no closing quote, so `_field` returned null
// for `program`, and `globalName` — which follows it — never entered the
// window at all. One response in eight failed that way; each failure
// dropped the token to the InnerTube challenge and killed the stream at
// ~60s (stream protection 2 -> 3).
//
// Brace matching has no size to get wrong. Strings are tracked so a
// brace or a backslash inside a value cannot unbalance the count.
function _challengeObjects(full) {
  const key = '"bgChallenge":';
  const out = [];
  for (let at = full.indexOf(key); at >= 0; at = full.indexOf(key, at + 1)) {
    const start = full.indexOf('{', at + key.length);
    if (start < 0) continue;
    let depth = 0, inStr = false, esc = false;
    for (let i = start; i < full.length; i++) {
      const c = full[i];
      if (inStr) {
        if (esc) { esc = false; continue; }
        if (c === '\\') { esc = true; continue; }
        if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') { inStr = true; continue; }
      if (c === '{') depth++;
      else if (c === '}') {
        depth--;
        if (depth === 0) { out.push(full.slice(start, i + 1)); break; }
      }
    }
  }
  return out;
}

// Parse the BotGuard challenge out of the homepage HTML, together with
// that page's EVENT_ID.
//
// Why the homepage matters: SABR verifies that the PO Token came from a
// snapshot that carries the page context it was minted against. A token
// from an InnerTube challenge has no such context, so the stream runs on
// the cold-start allowance and dies at ~60s with stream protection status
// 2 -> 3. Measured both ways on the same page-native challenge: without
// EVENT_ID status 2 at ~60s, with it status 1 past 65s (PipePipeClient
// PR #86, "use the page-native BotGuard attestation context").
//
// Split out from the fetch so it can be tested against fixed HTML: the
// way this parse fails is a property of the markup, not of the network.
// (`challenge` is deliberately not part of the result — it sits next to
// bgChallenge rather than inside it, and nothing downstream reads it.)
export function parsePageContext(html) {
  const eventId = _field(html, 'EVENT_ID');
  const objects = _challengeObjects(_decodeEscapes(html));
  if (!objects.length) throw new Error('no bgChallenge on homepage');

  // A page can mention bgChallenge more than once. Take the mention that
  // actually carries the fields rather than assuming the first one is it.
  let best = null;
  for (const obj of objects) {
    const fields = {
      program: _field(obj, 'program'),
      globalName: _field(obj, 'globalName'),
      interpreterUrl: _field(
        obj, 'privateDoNotAccessOrElseTrustedResourceUrlWrappedValue'),
    };
    if (!best) best = fields;
    if (fields.program && fields.globalName && fields.interpreterUrl) {
      best = fields;
      break;
    }
  }
  if (!eventId || !best.program || !best.globalName || !best.interpreterUrl)
    throw new Error('homepage context incomplete '
      + `(eventId=${!!eventId} program=${!!best.program} `
      + `globalName=${!!best.globalName} interpreterUrl=${!!best.interpreterUrl})`);
  return { eventId, ...best };
}

// The parse has no size ceiling any more, but the homepage still varies
// between responses — a consent page, a reshuffled config, a different
// field set — and a single unlucky fetch costs the whole video, which
// then dies at ~60s. One extra request is cheap next to that.
const PAGE_CTX_ATTEMPTS = 3;

async function _fetchPageContext() {
  let lastErr;
  for (let attempt = 1; attempt <= PAGE_CTX_ATTEMPTS; attempt++) {
    try {
      const res = await fetch(HOMEPAGE, {
        headers: { 'user-agent': REAL_UA, 'accept-language': 'en-US,en;q=0.9' },
      });
      if (!res.ok) throw new Error(`homepage HTTP ${res.status}`);
      return parsePageContext(await res.text());
    } catch (e) {
      lastErr = e;
      if (attempt < PAGE_CTX_ATTEMPTS)
        log(`page context attempt ${attempt}/${PAGE_CTX_ATTEMPTS} failed `
            + `(${e.message}) — retrying`);
    }
  }
  throw lastErr;
}

async function _runChallenge(visitorData) {
  // jsdom gives us document, navigator, requestAnimationFrame etc. that
  // BotGuard's interpreter expects. Pretending to be a real browser at
  // youtube.com matters: BotGuard inspects `location`, `document.cookie`,
  // and similar surfaces during the snapshot.
  // The default jsdom UA self-identifies as `jsdom/X.Y` — BotGuard
  // checks navigator.userAgent and silently refuses to wire up the
  // mint callback when it sees that. Override to a plausible Chrome.
  const dom = new JSDOM(
    '<!DOCTYPE html><html><head></head><body></body></html>',
    {
      url: HOMEPAGE,
      referrer: HOMEPAGE,
      userAgent: REAL_UA,
      pretendToBeVisual: true,
      runScripts: 'outside-only',
    },
  );
  const w = dom.window;
  w.self = w;
  w.globalThis = w;

  let pageCtx = null;
  try {
    if (process.env.YOUHUB_NO_PAGE_CTX) throw new Error('disabled by YOUHUB_NO_PAGE_CTX');
    log('fetching page-native challenge from youtube.com…');
    pageCtx = await _fetchPageContext();
    log(`page context ok (EVENT_ID=${pageCtx.eventId}, `
        + `globalName=${pageCtx.globalName})`);
  } catch (e) {
    // Deliberately loud: this is the one line that explains a stream that
    // dies at ~60s with stream protection status 3, and it was previously
    // buried among the per-segment protection chatter.
    log(`FALLBACK: no page-native context (${e.message}) — using the `
        + 'InnerTube challenge; that token carries no page context, so '
        + 'expect the stream to die at ~60s');
  }

  let program, globalName, interpreterUrl;
  if (pageCtx) {
    ({ program, globalName, interpreterUrl } = pageCtx);
    // BotGuard reads the page identity off the window it snapshots. With
    // no window.yt.config_.EVENT_ID the snapshot describes a page that
    // never existed, and SABR refuses the token after the cold start.
    w.yt = { config_: { EVENT_ID: pageCtx.eventId } };
  } else {
    log('fetching challenge via InnerTube…');
    const bgChallenge = await _fetchChallengeViaInnertube();
    interpreterUrl = bgChallenge.interpreter_url
      .private_do_not_access_or_else_trusted_resource_url_wrapped_value;
    program = bgChallenge.program;
    globalName = bgChallenge.global_name;
  }
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
