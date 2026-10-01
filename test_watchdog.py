#!/usr/bin/env python3.11
"""Deterministic tests for LivePlayer._watchdog_loop's reopen decisions.

The real loop is bound to a stub, so nothing waits on YouTube or on a
player: the two numbers that decide everything — how much media is on
disk (`media_end`) and where the playhead is (`_last_pos_sec`) — are
supplied directly.

Run:  .venv/bin/python3.11 test_watchdog.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import bridge_player as bp  # noqa: E402


class Stub:
    """Only the attributes _watchdog_loop actually touches.

    Do not add fields of your own: the method reads exactly these, and
    extra ones just hide the coupling.
    """

    def __init__(self, tmpfile: Path, log_path: Path, media_end: float,
                 playhead: float, duration: float):
        self.log_path = log_path
        self.tmpfile = tmpfile
        self._shutdown = threading.Event()
        self._restart_lock = threading.Lock()
        self._last_pos_sec = playhead
        self._duration_sec = duration
        self._file_start_sec = 0.0
        self._stall_failures = 0
        self.restarts: list[float] = []
        self._media_end_sec = lambda: media_end

    def _restart_at(self, target: float, cprint) -> None:
        self.restarts.append(target)


def run(media_end: float, playhead: float, duration: float,
        seconds: float = 0.7):
    """Run the watchdog for a moment against a file that never grows."""
    tmp = Path(tempfile.mkstemp(prefix="wd_frozen_", suffix=".mkv")[1])
    tmp.write_bytes(b"x" * 1024)
    log_path = Path(tempfile.mkstemp(prefix="wd_log_", suffix=".log")[1])
    try:
        s = Stub(tmp, log_path, media_end, playhead, duration)
        t = threading.Thread(target=lambda: bp.LivePlayer._watchdog_loop(s),
                             daemon=True)
        t.start()
        time.sleep(seconds)
        s._shutdown.set()
        t.join(timeout=2)
        return s.restarts, log_path.read_text(errors="replace")
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    # Small timings so the tests finish in a second or two.
    bp.STALL_TIMEOUT_SEC = 0.15
    bp.STALL_POLL_SEC = 0.03
    bp.STALL_WAIT_MAX_SEC = 0.4

    fails = 0

    # 1. The reported bug: the whole video is on disk (299.9 of 300) and
    #    the playhead is still near the start, because a session delivers
    #    its entire burst at once. This must NOT be mistaken for a dead
    #    stream — reopening at 299.9s only earns "Missing segments".
    restarts, log = run(299.9, 16.2, 300.0)
    ok = not restarts and "nothing to reopen" in log
    print(f"{'PASS' if ok else 'FAIL'}  finished video (299.9/300, head 16.2s)"
          f" -> restarts={restarts}")
    fails += not ok

    # 2. Control: a genuine mid-video stall still reopens, at the end of
    #    the media that is actually on disk.
    restarts, log = run(61.0, 61.0, 300.0)
    ok = bool(restarts) and abs(restarts[0] - 61.0) < 0.01
    print(f"{'PASS' if ok else 'FAIL'}  dead stream (61.0/300, head 61s)"
          f" -> restarts={restarts}")
    fails += not ok

    # 3. Control: while the player still holds buffered media, wait for it
    #    to drain instead of throwing it away.
    restarts, log = run(61.0, 10.0, 300.0, seconds=0.5)
    ok = not restarts and "letting the buffer play out" in log
    print(f"{'PASS' if ok else 'FAIL'}  buffered (61.0/300, head 10s)"
          f" -> restarts={restarts}")
    fails += not ok

    print("\nall passed" if not fails else f"\n{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
