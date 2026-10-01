#!/usr/bin/env node
// Deterministic tests for the BotGuard page-context parse.
//
// No network: the fixture is the shape the homepage actually serves —
// the challenge config wrapped in a JS string literal, so every quote
// arrives as \x22 and the parser has to decode it first.
//
// The case that matters is the one where `program` runs past whatever
// window the parser slices around `bgChallenge`. That is not a
// hypothetical: the window was 40 000 characters and the real program
// measured 38 695-39 111 normally but 40 371 and 42 899 on responses
// that broke playback. When the window cuts the value, `program` loses
// its closing quote and `globalName`, which follows it, falls outside
// entirely — the parse then falls back to the InnerTube challenge and
// the stream dies at ~60s.
import { parsePageContext } from './po_token.mjs';

let failures = 0;
let checks = 0;

function check(name, fn) {
  checks++;
  try {
    const detail = fn();
    console.log(`PASS  ${name}${detail ? `  — ${detail}` : ''}`);
  } catch (e) {
    failures++;
    console.log(`FAIL  ${name}  — ${e.message}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// Deterministic base64-ish filler — no Math.random, so a failure is
// always reproducible.
function filler(len) {
  const alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let out = '';
  let x = 123456789;
  for (let i = 0; i < len; i++) {
    x = (1103515245 * x + 12345) & 0x7fffffff;
    out += alpha[x % alpha.length];
  }
  return out;
}

// The homepage escapes the config as a JS string: quotes become \x22.
function escapeQuotes(s) {
  return s.replace(/"/g, '\\x22');
}

function homepage({ programLen, globalName = 'trayride', programText,
                    extraFields = '', challenge = 'a=6\\&a2=10\\&b=ORCc' }) {
  const program = programText !== undefined ? programText : filler(programLen);
  const config =
    '{"challenge":"' + challenge + '",'
    + '"bgChallenge":{"interpreterUrl":{"privateDoNotAccessOrElseTrusted'
    + 'ResourceUrlWrappedValue":"//www.google.com/js/th/3sGYGc4hYgVHYZEI2'
    + 'lPZZTlOvxH6DVRbT7JLSrTea7w.js"},'
    + '"interpreterHash":"3sGYGc4hYgVHYZEI2lPZZTlOvxH6DVRbT7JLSrTea7w",'
    + '"program":"' + program + '",'
    + '"globalName":"' + globalName + '"'
    + extraFields + '}}';
  // EVENT_ID is read from the undecoded page, the way ytcfg carries it.
  return '<!DOCTYPE html><html><head>'
    + '<script>ytcfg.set({"EVENT_ID":"PB--aoHbG9GAl9EPsY-E2Q0",'
    + '"XSRF_TOKEN":"x"});</script>'
    + '</head><body><script>var ytInitialPlayerResponse = '
    + escapeQuotes(config)
    + ';</script></body></html>';
}

// --- the regression: program longer than any fixed slice ---

check('program of 38 900 chars (the normal size) parses',
  () => {
    const ctx = parsePageContext(homepage({ programLen: 38900 }));
    assert(ctx.program.length === 38900, `program got ${ctx.program.length} chars`);
    assert(ctx.globalName === 'trayride', `globalName=${ctx.globalName}`);
    return `program=${ctx.program.length} globalName=${ctx.globalName}`;
  });

check('program of 40 371 chars (measured on a failing response) parses',
  () => {
    const ctx = parsePageContext(homepage({ programLen: 40371 }));
    assert(ctx.program.length === 40371, `program got ${ctx.program.length} chars`);
    assert(ctx.globalName === 'trayride', `globalName=${ctx.globalName}`);
    return `program=${ctx.program.length} globalName=${ctx.globalName}`;
  });

check('program of 42 899 chars (measured on a failing response) parses',
  () => {
    const ctx = parsePageContext(homepage({ programLen: 42899 }));
    assert(ctx.program.length === 42899, `program got ${ctx.program.length} chars`);
    assert(ctx.globalName === 'trayride', `globalName=${ctx.globalName}`);
    return `program=${ctx.program.length} globalName=${ctx.globalName}`;
  });

check('program of 200 000 chars parses (no size ceiling at all)',
  () => {
    const ctx = parsePageContext(homepage({ programLen: 200000 }));
    assert(ctx.program.length === 200000, `program got ${ctx.program.length} chars`);
    return `program=${ctx.program.length} globalName=${ctx.globalName}`;
  });

// --- the parse must still read the other fields off the same object ---

check('interpreterUrl and EVENT_ID come through',
  () => {
    const ctx = parsePageContext(homepage({ programLen: 39000 }));
    assert(ctx.interpreterUrl.endsWith('3sGYGc4hYgVHYZEI2lPZZTlOvxH6DVRbT7JLSrTea7w.js'),
           `interpreterUrl=${ctx.interpreterUrl}`);
    assert(ctx.eventId === 'PB--aoHbG9GAl9EPsY-E2Q0', `eventId=${ctx.eventId}`);
    return 'interpreterUrl ok, eventId ok';
  });

check('braces and backslashes inside string values do not derail it',
  () => {
    const ctx = parsePageContext(homepage({
      programLen: 41000,
      extraFields: ',"note":"a } brace and \\\\ backslash in a value"',
    }));
    assert(ctx.program.length === 41000, `program got ${ctx.program.length} chars`);
    assert(ctx.globalName === 'trayride', `globalName=${ctx.globalName}`);
    return 'fields after a value containing } and \\';
  });

// --- failures must stay loud, not silently return a half-filled object ---

check('a config with no globalName is rejected, not half-returned',
  () => {
    const html = homepage({ programLen: 39000 })
      .replace(/\\x22globalName\\x22/, '\\x22globalNameRenamed\\x22');
    let threw = null;
    try { parsePageContext(html); } catch (e) { threw = e; }
    assert(threw, 'parse succeeded where globalName was missing');
    assert(/context incomplete/.test(threw.message), `unexpected error: ${threw.message}`);
    return threw.message;
  });

check('a page with no bgChallenge at all is rejected',
  () => {
    let threw = null;
    try { parsePageContext('<html><body>nothing here</body></html>'); }
    catch (e) { threw = e; }
    assert(threw, 'parse succeeded on a page with no challenge');
    assert(/no bgChallenge/.test(threw.message), `unexpected error: ${threw.message}`);
    return threw.message;
  });

console.log(failures === 0
  ? `\nall ${checks} checks passed`
  : `\n${failures} of ${checks} checks failed`);
process.exit(failures === 0 ? 0 : 1);
