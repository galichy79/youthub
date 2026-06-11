// SABR streaming bridge — persistent mode for fast seek.
//
// Usage:
//   node sabr_bridge.mjs <video_id> --control <unix_socket_path>
//   (legacy one-shot:  node sabr_bridge.mjs <video_id>           — muxed to stdout)
//
// Persistent mode is what bridge_player.py uses now. The bridge loads
// the heavy stuff (playerResponse, youtubei.js decipher, PO Token,
// chosen itags, googlevideo SabrStream object) exactly once, then
// waits for control-socket commands:
//
//   START_SESSION path=<output_file> start_at=<seconds>
//     — abort current SabrStream + ffmpeg child, spawn new ones,
//       write muxed matroska to <output_file>. Reply: `OK session=<id>`
//       once first bytes hit disk. Or `ERR <reason>` on failure.
//   STOP_SESSION
//     — abort current session, leave the bridge running for a future
//       START_SESSION. Reply: `OK`.
//   QUIT
//     — abort + exit(0).
//
// One client connection at a time; reconnects are fine.
//
// Why this exists: each seek-anywhere used to kill the bridge process
// and start fresh, paying ~3 s of Node startup + imports + decipher +
// /watch fetch on every seek. With persistent, the only cost on seek
// is one SABR roundtrip + matroska header bytes hitting disk (~1 s).

import { SabrStream } from 'googlevideo/sabr-stream';
import { Innertube } from 'youtubei.js';
import { Platform } from './node_modules/youtubei.js/dist/src/utils/Utils.js';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs';
import net from 'node:net';
import process from 'node:process';
import { getPoToken } from './po_token.mjs';

const execFileP = promisify(execFile);

// youtubei.js refuses to decipher signatures without a JS evaluator.
function nodeEvaluate(data, _env) {
  // eslint-disable-next-line no-new-func
  return (new Function(data.output))();
}
Platform.shim.eval = nodeEvaluate;

const OAUTH_CACHE = `${process.cwd()}/cache/oauth.json`;

// --------------------------- CLI parsing ---------------------------

const rawArgs = process.argv.slice(2);
let controlSocketPath = null;
let videoFifo = null, audioFifo = null;  // legacy mode
const positional = [];
for (let i = 0; i < rawArgs.length; i++) {
  const a = rawArgs[i];
  if (a === '--control') controlSocketPath = rawArgs[++i];
  else positional.push(a);
}
const [videoId, legacyVideoFifo, legacyAudioFifo] = positional;
videoFifo = legacyVideoFifo; audioFifo = legacyAudioFifo;
if (!videoId) {
  console.error('usage: node sabr_bridge.mjs <video_id> [--control <socket>]');
  console.error('       node sabr_bridge.mjs <video_id> <v_fifo> <a_fifo>   (legacy)');
  process.exit(2);
}
const persistent = !!controlSocketPath;

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function log(msg) { process.stderr.write(`${ts()} [bridge] ${msg}\n`); }

// --------------------------- shared helpers ---------------------------

function extractPlayerResponse(html) {
  const patterns = [
    /var ytInitialPlayerResponse\s*=\s*({.+?})\s*;\s*var/s,
    /ytInitialPlayerResponse\s*=\s*({.+?})\s*;\s*<\/script>/s,
    /ytInitialPlayerResponse"\s*:\s*({.+?}),\s*"ytInitialData"/s,
  ];
  for (const re of patterns) {
    const m = html.match(re);
    if (m) { try { return JSON.parse(m[1]); } catch { /* try next */ } }
  }
  return null;
}

function toNum(v) {
  if (v === undefined || v === null) return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

function toSabrFormat(f) {
  return {
    itag: f.itag, lastModified: f.lastModified, xtags: f.xtags,
    width: f.width, height: f.height,
    contentLength: toNum(f.contentLength),
    audioTrackId: f.audioTrack?.id, mimeType: f.mimeType, isDrc: f.isDrc,
    quality: f.quality, qualityLabel: f.qualityLabel,
    averageBitrate: toNum(f.averageBitrate),
    bitrate: toNum(f.bitrate) ?? 0,
    audioQuality: f.audioQuality,
    approxDurationMs: toNum(f.approxDurationMs) ?? 0,
    language: f.language, isDubbed: f.isDubbed, isOriginal: f.isOriginal,
    // The audio dub picker (multi-track videos) depends on these:
    audioIsDefault: f.audioTrack?.audioIsDefault === true,
    audioTrackDisplayName: f.audioTrack?.displayName,
  };
}

async function loadCachedPlayerResponse(vid) {
  try {
    const raw = await fs.promises.readFile(
      `cache/bootstrap_${vid}.player.json`, 'utf8');
    return JSON.parse(raw);
  } catch { return null; }
}

// Bridge-side PR cache. Independent of Camoufox bootstrap cache —
// we save here whenever a /watch fetch succeeds, so future runs that
// hit YT's bot wall can fall back to a still-fresh PR (and to a
// still-valid SABR URL: `expire=` is good for ~17 h after fetch).
async function loadBridgePrCache(vid, maxAgeSec = 12 * 3600) {
  try {
    const path = `cache/pr_${vid}.json`;
    const stat = await fs.promises.stat(path);
    const ageSec = (Date.now() - stat.mtimeMs) / 1000;
    if (ageSec > maxAgeSec) return null;
    const raw = await fs.promises.readFile(path, 'utf8');
    return JSON.parse(raw);
  } catch { return null; }
}

async function saveBridgePrCache(vid, pr) {
  try {
    await fs.promises.mkdir('cache', { recursive: true });
    await fs.promises.writeFile(
      `cache/pr_${vid}.json`, JSON.stringify(pr));
  } catch (e) {
    log(`failed to save PR cache: ${e?.message ?? e}`);
  }
}

// Fetch playerResponse via the pr_fetch.py sidecar. pr_fetch is an
// atomic single-strategy primitive — it tries ONE (TLS profile × IP)
// combination from its rotation, marks it dead on bot-wall, and exits.
// Walking the whole rotation in one user-visible click happens here:
// we loop pr_fetch until a strategy returns streamingData or until the
// full rotation (MAX_ATTEMPTS) is exhausted. pr_fetch's `current`
// pointer is sticky on success, so the first iteration always tries
// whatever worked last (same TLS + same IP) before moving on.
//
// MAX_ATTEMPTS must stay aligned with pr_fetch.STRATEGIES length
// (12 bases × {direct, proxy} = 24). Going higher just means we keep
// retrying after a full rotation has been resurrected — diminishing
// returns; if 24 distinct (fingerprint, IP) combos all failed, the
// rate-limit is real and waiting is the only fix.
const PR_FETCH_MAX_ATTEMPTS = 24;
const PR_FETCH_TIMEOUT_MS = 25_000;

async function fetchWatchPlayerResponse(vid) {
  const py = `${process.cwd()}/.venv/bin/python3.11`;
  const script = `${process.cwd()}/pr_fetch.py`;
  log(`/watch via pr_fetch (rotation, up to ${PR_FETCH_MAX_ATTEMPTS} strategies)`);
  let lastReason = 'no attempt';
  for (let attempt = 1; attempt <= PR_FETCH_MAX_ATTEMPTS; attempt++) {
    try {
      const t0 = Date.now();
      const { stdout, stderr } = await execFileP(py, [script, vid], {
        timeout: PR_FETCH_TIMEOUT_MS,
        maxBuffer: 8 * 1024 * 1024,
        env: process.env,  // pass HTTPS_PROXY through
      });
      const ms = Date.now() - t0;
      // pr_fetch announces its pick on stderr ("using <id> (round N)").
      // Surface it on success too — otherwise the log only ever names
      // strategies when they fail, and "which combo actually works"
      // is invisible when debugging.
      const strat = stderr?.match(/using (\S+ \(round \d+\))/)?.[1] ?? '?';
      let fetched;
      try { fetched = JSON.parse(stdout); }
      catch (e) {
        lastReason = `JSON parse failed: ${e?.message ?? e}`;
        log(`attempt ${attempt}/${PR_FETCH_MAX_ATTEMPTS}: ${lastReason}`);
        continue;
      }
      if (fetched && fetched.streamingData) {
        log(`pr_fetch ok in ${ms}ms (attempt ${attempt}/${PR_FETCH_MAX_ATTEMPTS}, ${strat})`);
        return fetched;
      }
      // Strategy completed the request but YT returned an unplayable
      // payload (login wall, age gate, geo block, …). pr_fetch only
      // marks the strategy dead on bot-wall — other reasons leave it
      // alive, so the next iter would just retry the same strategy.
      // Treat these as terminal for the whole loop: rotating won't
      // help if the *video* is the problem.
      lastReason = fetched?.playabilityStatus?.reason
        || fetched?.playabilityStatus?.status || 'no streamingData';
      log(`pr_fetch returned unplayable: ${lastReason} — stopping rotation`);
      break;
    } catch (e) {
      // execFileP throws on non-zero exit, timeout, or process error.
      // pr_fetch exits 2 on bot-wall AFTER marking the strategy dead
      // and advancing its `current` pointer, so the next iter
      // automatically picks a fresh (TLS, IP) combo.
      lastReason = e?.message ?? String(e);
      log(`attempt ${attempt}/${PR_FETCH_MAX_ATTEMPTS} died: ${e?.code || ''} ${lastReason.split('\n')[0]}`);
    }
  }
  log(`pr_fetch rotation exhausted (last reason: ${lastReason})`);
  // Last-ditch Node fetch with its own TLS fingerprint — different
  // code path; usually walled too but the extra error line in the log
  // is informative when debugging "everything failed" tickets.
  try {
    log(`fallback Node fetch`);
    const res = await fetch(`https://www.youtube.com/watch?v=${vid}`, {
      headers: {
        'User-Agent':
          'Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0',
        'Accept': 'text/html,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Cookie': 'CONSENT=YES+; SOCS=CAI',
      },
    });
    if (!res.ok) { log(`Node /watch HTTP ${res.status}`); return null; }
    const fetched = extractPlayerResponse(await res.text());
    if (fetched && fetched.streamingData) return fetched;
    log(`Node /watch unusable (${fetched?.playabilityStatus?.reason || 'no PR'})`);
  } catch (e) { log(`Node /watch threw: ${e?.message ?? e}`); }
  return null;
}

async function loadAccessToken() {
  try {
    const t = JSON.parse(await fs.promises.readFile(OAUTH_CACHE, 'utf8'));
    return t.access_token;
  } catch { return null; }
}

function extractPoTokenFromBootstrapBody(bin) {
  function readVarint(buf, pos) {
    let result = 0n, shift = 0n;
    while (true) {
      const b = buf[pos++];
      result |= BigInt(b & 0x7f) << shift;
      if ((b & 0x80) === 0) return [Number(result), pos];
      shift += 7n;
    }
  }
  let pos = 0;
  while (pos < bin.length) {
    const [tag, next] = readVarint(bin, pos); pos = next;
    const fieldNo = tag >>> 3, wire = tag & 7;
    if (wire === 0) { [, pos] = readVarint(bin, pos); }
    else if (wire === 2) {
      const [len, after] = readVarint(bin, pos);
      const payload = bin.subarray(after, after + len);
      if (fieldNo === 19) {
        let p2 = 0;
        while (p2 < payload.length) {
          const [innerTag, n2] = readVarint(payload, p2); p2 = n2;
          const innerField = innerTag >>> 3, innerWire = innerTag & 7;
          if (innerWire === 2) {
            const [innerLen, after2] = readVarint(payload, p2);
            if (innerField === 2) {
              return Buffer.from(payload.subarray(after2, after2 + innerLen))
                .toString('base64');
            }
            p2 = after2 + innerLen;
          } else if (innerWire === 0) { [, p2] = readVarint(payload, p2); }
          else { p2 += 4; }
        }
      }
      pos = after + len;
    } else { pos += (wire === 1) ? 8 : 4; }
  }
  return undefined;
}

// --------------------------- one-time init ---------------------------

const ctx = {
  videoId,
  pr: null,
  sabrUrl: null,
  ustreamerCfg: null,
  durationMs: 0,
  allFormats: null,
  poToken: undefined,
  accessToken: null,
  pickVideo: null,
  pickAudio: null,
};

async function initOnce() {
  // Three-tier playerResponse resolution:
  //   1. fresh /watch fetch (3 UA variants — YT bot scoring is jittery)
  //   2. our own bridge PR cache (saved last time /watch worked, 12 h TTL)
  //   3. old Camoufox bootstrap cache (legacy, usually absent now)
  // If all fail, throw — bridge_player surfaces the error to the user.
  ctx.pr = await fetchWatchPlayerResponse(videoId);
  if (ctx.pr) {
    log(`got fresh playerResponse from /watch`);
    saveBridgePrCache(videoId, ctx.pr).catch(() => {});
  } else {
    ctx.pr = await loadBridgePrCache(videoId);
    if (ctx.pr) {
      log(`using bridge PR cache (fresh enough — SABR URL still valid)`);
    } else {
      ctx.pr = await loadCachedPlayerResponse(videoId);
      if (ctx.pr) {
        log(`using legacy bootstrap PR cache`);
      } else {
        throw new Error(
          'YT bot-wall: /watch refused and no cached PR. '
          + 'Likely your IP is temporarily rate-limited; '
          + 'wait 15-60 min or change the proxy and retry. '
          + 'A successful playback in the next window will warm the '
          + 'cache so the bot-walled state becomes survivable.');
      }
    }
  }
  const ps = ctx.pr.playabilityStatus, sd = ctx.pr.streamingData;
  log(`playability: ${ps?.status}`);
  if (!sd) throw new Error(`no streamingData (reason: ${ps?.reason})`);

  const rawSabr = sd.serverAbrStreamingUrl;
  ctx.ustreamerCfg = ctx.pr.playerConfig?.mediaCommonConfig
    ?.mediaUstreamerRequestConfig?.videoPlaybackUstreamerConfig;
  ctx.allFormats = (sd.adaptiveFormats ?? []).map(toSabrFormat);
  ctx.durationMs = Number(ctx.pr.videoDetails?.lengthSeconds ?? 0) * 1000;
  log(`adaptive formats: ${ctx.allFormats.length}  duration: ${ctx.durationMs}ms`);

  {
    // PR's SABR URL has encrypted `n` and `sig` query params regardless
    // of whether the PR came from /watch or from cache — both contain
    // the raw raw ytInitialPlayerResponse. Always run n-decipher.
    log('loading youtubei.js for n-decipher…');
    const yt = await Innertube.create({
      client_type: 'WEB', generate_session_locally: true,
    });
    ctx.sabrUrl = await yt.session.player.decipher(rawSabr);
  }
  log(`sabr URL ready (${ctx.sabrUrl.length}B)`);

  // PO Token via bgutils (no browser). Bind to visitorData from this
  // playerResponse so the token + URL share the same session identity.
  const visitorData = ctx.pr.responseContext?.visitorData
    || ctx.pr.responseContext?.serviceTrackingParams
        ?.flatMap(s => s.params || [])
        ?.find(p => p.key === 'visitor_data')?.value;
  if (visitorData) {
    try {
      // SABR /videoplayback expects a content-bound PO Token (bound
      // to videoId, not visitorData). visitorData is just the session
      // identity used during integrity-token issuance.
      log(`generating PO Token (bgutils) bound to videoId=${videoId}`);
      const t0 = Date.now();
      ctx.poToken = await getPoToken(visitorData, videoId);
      log(`PO Token ready: ${ctx.poToken.length}B in ${Date.now()-t0}ms`);
    } catch (e) {
      log(`bgutils PO Token failed: ${e?.message ?? e}`);
    }
  } else {
    log('no visitorData in playerResponse — skipping PO Token');
  }

  // Legacy fallback: if bgutils failed, try the Camoufox-captured body.
  if (!ctx.poToken) {
    try {
      const bin = await fs.promises.readFile(
        `cache/bootstrap_${videoId}.body.bin`);
      ctx.poToken = extractPoTokenFromBootstrapBody(bin);
      if (ctx.poToken) log(`PO Token from cache: ${ctx.poToken.length}B`);
    } catch (e) { log(`no PO Token cache: ${e.message ?? e}`); }
  }

  ctx.accessToken = await loadAccessToken();

  // Video itag priority. H.264 first at every resolution because:
  // software VP9 decode tops out at ~75-100 fps for 1080p on a typical
  // desktop CPU, which is below the ~120 fps needed for 2x playback
  // speed. H.264 decodes ~2-3x faster, leaving headroom for the atempo
  // speedup path. We lose ~30% codec efficiency vs VP9 (= bigger
  // segments) but we're streaming live, not saving to disk.
  //   299 = H.264 1080p60
  //   303 = VP9   1080p60
  //   137 = H.264 1080p30
  //   248 = VP9   1080p30
  //   136 = H.264  720p
  //   247 = VP9    720p
  const itags = ctx.allFormats.map(f => f.itag);
  ctx.pickVideo = [299, 303, 137, 248, 136, 247, 135, 134, 133]
    .find(t => itags.includes(t));

  // Audio: multi-track videos (with dubs/originals) have several
  // formats per itag — one per language. SabrStream.chooseFormat
  // accepts either an itag number (first match wins, NOT
  // language-aware) or a full format object (used as-is). Pick the
  // object ourselves. Priority for a Russian-speaking user:
  //   1. Russian dub (audioTrackId starts with 'ru') — explicit Russian
  //      track, exists when an English video has a RU dub overlay.
  //   2. audioIsDefault — original-language track. Used when no RU dub
  //      exists; for Russian-origin videos this IS the Russian track.
  //   3. anything — last-resort.
  // Within candidates, prefer Opus high > Opus med > AAC.
  const audioItagPrio = [251, 250, 249, 140];
  const audioCandidates = ctx.allFormats.filter(
    f => f.mimeType?.includes('audio')
      && audioItagPrio.includes(f.itag),
  );
  function pickBest(filter) {
    const matching = audioCandidates.filter(filter);
    matching.sort((a, b) =>
      audioItagPrio.indexOf(a.itag) - audioItagPrio.indexOf(b.itag));
    return matching[0];
  }
  ctx.pickAudio =
       pickBest(f => (f.audioTrackId || '').startsWith('ru'))
    || pickBest(f => f.audioIsDefault === true)
    || pickBest(_ => true);

  if (!ctx.pickVideo || !ctx.pickAudio)
    throw new Error('no usable itag pair');
  const at = ctx.pickAudio.audioTrackId || '?';
  log(`picked: video=itag ${ctx.pickVideo}  audio=itag ${ctx.pickAudio.itag} `
      + `track=${at} (default=${!!ctx.pickAudio.audioIsDefault})`);
}

// --------------------------- session lifecycle ---------------------------

let currentSession = null;
let sessionCounter = 0;

async function stopSession() {
  if (!currentSession) return;
  const s = currentSession;
  currentSession = null;
  log(`stopping session #${s.id}`);
  try { s.sabr?.abort(); } catch { /* */ }
  try {
    s.videoOut?.end();
    s.audioOut?.end();
  } catch { /* */ }
  // Give ffmpeg ~1s to flush, then kill if still alive.
  if (s.ffmpeg && s.ffmpeg.exitCode === null) {
    const exited = await new Promise(res => {
      const t = setTimeout(() => res(false), 1500);
      s.ffmpeg.once('exit', () => { clearTimeout(t); res(true); });
    });
    if (!exited) { try { s.ffmpeg.kill('SIGKILL'); } catch { /* */ } }
  }
  log(`session #${s.id} stopped`);
}

async function startSession({ outputPath, startAtSec }) {
  // initOnce() may still be in flight if this is the very first
  // session — block until it's done. Subsequent sessions return
  // immediately because initReady is already resolved.
  if (initReady) await initReady;
  await stopSession();
  const id = ++sessionCounter;
  const startAtMs = Math.max(0, Math.floor(startAtSec * 1000));
  log(`session #${id} → ${outputPath}  start_at=${startAtMs}ms`);

  // Authenticated fetch for SABR (Bearer makes the request trusted).
  const authFetch = (url, init = {}) => {
    const headers = new Headers(init.headers ?? {});
    if (ctx.accessToken)
      headers.set('Authorization', `Bearer ${ctx.accessToken}`);
    if (!headers.has('User-Agent')) {
      headers.set('User-Agent',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        + '(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36');
    }
    return fetch(url, { ...init, headers });
  };

  // ffmpeg child writes directly to outputPath so we don't shuttle the
  // muxed bytes through Node → Python → file.
  // -copyts: preserve absolute PTS from SabrStream segments so ffplay's
  // master clock reports real video time. Without it bridge_player's
  // seek bookkeeping (in_range check, _file_start_sec) gets confused.
  //
  // DO NOT add `-fflags +nobuffer` here — it makes ffmpeg skip the
  // input init buffer where the first I-frame lives, so the output
  // starts at the NEXT keyframe (~3 sec in) and ffplay shows a blank
  // catch-up at startup. -flush_packets 1 alone is enough for the
  // low-latency goal we actually need.
  const ffmpeg = spawn('ffmpeg', [
    '-hide_banner', '-loglevel', 'warning',
    '-flush_packets', '1',
    '-thread_queue_size', '16384', '-i', 'pipe:3',
    '-thread_queue_size', '16384', '-i', 'pipe:4',
    '-copyts',
    '-map', '0:v:0', '-map', '1:a:0',
    '-c', 'copy',
    '-f', 'matroska', '-y', outputPath,
  ], { stdio: ['ignore', 'inherit', 'inherit', 'pipe', 'pipe'] });
  ffmpeg.on('exit', (code, sig) =>
    log(`session #${id} ffmpeg exit code=${code} sig=${sig}`));

  const sabr = new SabrStream({
    fetch: authFetch,
    serverAbrStreamingUrl: ctx.sabrUrl,
    videoPlaybackUstreamerConfig: ctx.ustreamerCfg,
    poToken: ctx.poToken,
    clientInfo: { clientName: 1, clientVersion: '2.20260206.01.00' },
    durationMs: ctx.durationMs,
    formats: ctx.allFormats,
  });
  sabr.on('streamProtectionStatusUpdate', s =>
    log(`session #${id} stream protection ${JSON.stringify(s)}`));
  sabr.on('error', e => log(`session #${id} sabr error: ${e?.message ?? e}`));

  // videoFormat = itag number (lets SabrStream pick the only matching
  // entry). audioFormat = full SabrFormat object so the correct dub
  // track (Russian / default) is taken instead of "first itag=251".
  const { videoStream, audioStream } = await sabr.start({
    videoFormat: ctx.pickVideo, audioFormat: ctx.pickAudio, startAtMs,
  });

  const videoOut = ffmpeg.stdio[3];
  const audioOut = ffmpeg.stdio[4];

  async function pump(name, readable, writable) {
    const reader = readable.getReader();
    let total = 0;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (!writable.write(value)) {
          await new Promise(r => writable.once('drain', r));
        }
      }
    } catch (e) { log(`session #${id} ${name} pump error: ${e?.message ?? e}`); }
    finally {
      log(`session #${id} ${name} pump done, ${total}B`);
      try { writable.end(); } catch { /* */ }
    }
  }

  // Pumps run in the background. We don't await them here — the
  // session is considered "started" as soon as ffmpeg writes its
  // first bytes to disk. bridge_player polls the file for that.
  Promise.all([
    pump('video', videoStream, videoOut),
    pump('audio', audioStream, audioOut),
  ]).catch(e => log(`session #${id} pumps crashed: ${e?.message ?? e}`));

  currentSession = { id, sabr, ffmpeg, videoOut, audioOut, outputPath };
  return id;
}

// --------------------------- control protocol ---------------------------

function parseKV(s) {
  // "key1=val1 key2=val2"
  const out = {};
  s.trim().split(/\s+/).forEach(p => {
    const eq = p.indexOf('=');
    if (eq > 0) out[p.slice(0, eq)] = p.slice(eq + 1);
  });
  return out;
}

async function handleControlLine(line, write) {
  const trimmed = line.trim();
  if (!trimmed) return;
  if (trimmed.startsWith('START_SESSION ')) {
    const kv = parseKV(trimmed.slice('START_SESSION '.length));
    if (!kv.path) return write('ERR missing path\n');
    try {
      const id = await startSession({
        outputPath: kv.path,
        startAtSec: Number(kv.start_at || '0'),
      });
      write(`OK session=${id}\n`);
    } catch (e) {
      write(`ERR ${e?.message ?? e}\n`);
    }
  } else if (trimmed === 'STOP_SESSION') {
    await stopSession();
    write('OK\n');
  } else if (trimmed === 'QUIT') {
    write('OK\n');
    await stopSession();
    process.exit(0);
  } else if (trimmed === 'STATUS') {
    write(`OK session=${currentSession?.id ?? 'none'}\n`);
  } else {
    write(`ERR unknown: ${trimmed}\n`);
  }
}

async function startControlServer() {
  try { fs.unlinkSync(controlSocketPath); } catch { /* */ }
  const server = net.createServer(sock => {
    log('control client connected');
    let buf = '';
    sock.on('data', chunk => {
      buf += chunk.toString('utf8');
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        handleControlLine(line, s => { try { sock.write(s); } catch { /* */ } })
          .catch(e => log(`control handler error: ${e?.message ?? e}`));
      }
    });
    sock.on('close', () => log('control client disconnected'));
    sock.on('error', e => log(`control socket error: ${e?.message ?? e}`));
  });
  await new Promise((res, rej) => {
    server.once('error', rej);
    server.listen(controlSocketPath, () => res());
  });
  log(`control socket: ${controlSocketPath}`);
}

// --------------------------- legacy one-shot ---------------------------

async function legacyOneShotMain() {
  // Behaves like the pre-persistent bridge: write muxed mkv to stdout
  // (or raw streams to two fifos). Used by smoke-tests and old paths.
  await initOnce();
  const outputPath = videoFifo && audioFifo
    ? null  // raw fifo mode below
    : 'pipe:1';

  if (outputPath === null) {
    // Two raw fifos — used by debugging scripts only.
    const sabr = new SabrStream({
      fetch: (u, i) => fetch(u, i),
      serverAbrStreamingUrl: ctx.sabrUrl,
      videoPlaybackUstreamerConfig: ctx.ustreamerCfg,
      poToken: ctx.poToken,
      clientInfo: { clientName: 1, clientVersion: '2.20260206.01.00' },
      durationMs: ctx.durationMs,
      formats: ctx.allFormats,
    });
    const { videoStream, audioStream } = await sabr.start({
      videoFormat: ctx.pickVideo, audioFormat: ctx.pickAudio,
    });
    const vOut = fs.createWriteStream(videoFifo);
    const aOut = fs.createWriteStream(audioFifo);
    const drain = async (name, r, w) => {
      const rd = r.getReader();
      for (;;) {
        const { value, done } = await rd.read();
        if (done) break;
        if (!w.write(value)) await new Promise(d => w.once('drain', d));
      }
      w.end();
    };
    await Promise.all([
      drain('video', videoStream, vOut), drain('audio', audioStream, aOut),
    ]);
    return;
  }

  // Single muxed stdout — spin up a session whose ffmpeg writes to pipe:1.
  // Simpler: reuse startSession with outputPath='pipe:1'? ffmpeg understands
  // pipe:1 as stdout, but spawn() wouldn't connect it. Use temporary file.
  const tmp = `/tmp/sabr_bridge_oneshot_${process.pid}.mkv`;
  await startSession({ outputPath: tmp, startAtSec: 0 });
  // Stream the file to stdout as it grows.
  const fd = await fs.promises.open(tmp, 'r');
  let pos = 0;
  while (currentSession) {
    const buf = Buffer.alloc(64 * 1024);
    const { bytesRead } = await fd.read(buf, 0, buf.length, pos);
    if (bytesRead > 0) {
      pos += bytesRead;
      process.stdout.write(buf.subarray(0, bytesRead));
    } else {
      await new Promise(r => setTimeout(r, 50));
    }
  }
  await fd.close();
  try { fs.unlinkSync(tmp); } catch { /* */ }
}

// --------------------------- main ---------------------------

// `initOnce()` does /watch fetch + n-decipher + bgutils PO Token — all
// sequential network/CPU work that can take 3-15 s depending on proxy
// latency. We don't make bridge_player wait for that: the control socket
// is opened immediately so bridge_player.connect() succeeds fast; the
// init promise is awaited inside startSession() when the first
// START_SESSION arrives (which has its own 30 s reply timeout).
let initReady = null;

(async () => {
  if (persistent) {
    initReady = initOnce().catch(err => {
      log(`initOnce failed: ${err?.stack ?? err}`);
      throw err;
    });
    await startControlServer();
    // keep alive
  } else {
    await legacyOneShotMain();
  }
})().catch(err => {
  log(`fatal: ${err?.stack ?? err}`);
  process.exit(1);
});

process.on('SIGTERM', async () => {
  await stopSession();
  process.exit(0);
});
process.on('SIGINT', async () => {
  await stopSession();
  process.exit(0);
});
