#!/usr/bin/env python3.11
"""Deterministic tests for LivePlayer._handle_seek_req's range decision.

The real method is bound to a stub, so nothing waits on YouTube or on a
player: the three numbers that decide everything — how much media is on
disk (`span`), where the playhead is (`pos`) and how big the file is
(`size`) — are supplied directly.

Run:  .venv/bin/python3.11 test_seek.py
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

MB = 1024 * 1024


class Stub:
    """Only the attributes _handle_seek_req actually touches.

    Do not add fields of your own: the method reads exactly these, and
    extra ones just hide the coupling.
    """

    # The real method under test, plus the cached-measurement wrapper it
    # calls; only the ffprobe call underneath is faked.
    _handle_seek_req = bp.LivePlayer._handle_seek_req
    _media_span_abs = bp.LivePlayer._media_span_abs

    def __init__(self, size_bytes: int, span, pos: float,
                 file_start: float = 0.0):
        self.tmpfile = Path(tempfile.mkstemp(prefix="seek_test_",
                                             suffix=".mkv")[1])
        with open(self.tmpfile, "wb") as f:
            f.truncate(size_bytes)          # sparse — instant at any size
        self._span = span
        self._last_pos_sec = pos
        self._file_start_sec = file_start
        self._last_seek_at = 0.0
        self._media_span = None
        self._pending_delta_sec = None
        self._restart_lock = threading.Lock()
        self._shutdown = threading.Event()
        self.ipc: list[str] = []
        self.restarts: list[float] = []
        self.log: list[str] = []

    # ---- the two seams ----
    def _probe_pts_span(self):
        return self._span

    def _send_ipc(self, line: str) -> bool:
        self.ipc.append(line)
        return True

    def _restart_at(self, target: float, cprint) -> None:
        self.restarts.append(target)

    # ---- driving ----
    def cprint(self, m: str) -> None:
        self.log.append(m)

    def press(self, delta: float, user_press: bool = True):
        bp.LivePlayer._handle_seek_req(self, delta, self.cprint, user_press)

    def settle(self, seconds: float = 1.0) -> None:
        """The restart path runs on a daemon thread."""
        deadline = time.time() + seconds
        while time.time() < deadline and not self.restarts and not self.ipc:
            time.sleep(0.01)

    def close(self) -> None:
        try: self.tmpfile.unlink()
        except OSError: pass


CASES: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CASES.append((f"{name}{('  — ' + detail) if detail else ''}", ok))


def run_case(name: str, *, size_mb: float, span, pos: float, delta: float,
             expect: str, file_start: float = 0.0) -> Stub:
    """expect: 'seek' (in-file SEEK_REL) or 'restart'."""
    s = Stub(int(size_mb * MB), span, pos, file_start)
    try:
        s.press(delta)
        s.settle()
        got = "seek" if s.ipc else ("restart" if s.restarts else "nothing")
        check(name, got == expect,
              f"expect={expect} got={got}  {s.log[-1] if s.log else ''}")
    finally:
        s.close()
    return s


def main() -> int:
    # 1. The reported bug: forward inside a file that holds far more
    #    media than the playhead has reached (play.log:6370 — 18.5 MB on
    #    disk, ~122 s of media, playhead at 4.8 s).
    run_case("forward inside downloaded (log:6370)",
             size_mb=18.5, span=(0.0, 122.0), pos=4.8, delta=+10.0,
             expect="seek")

    # 2. Backward must keep working exactly as before.
    run_case("backward inside downloaded (log:6490)",
             size_mb=249.0, span=(20.1, 1591.0), pos=213.2, delta=-10.0,
             expect="seek")

    # 3. Past the writer's edge is a real reason to restart.
    run_case("forward past downloaded edge",
             size_mb=4.0, span=(0.0, 30.0), pos=5.0, delta=+300.0,
             expect="restart")

    # 4. Both sides of the SEEK_SAFETY_BYTES boundary: 10 MB file,
    #    100 s of media on disk, so 1 s of media ≈ 0.1 MB and the test
    #    fires at 8.0 MB.
    run_case("target just inside the safety margin",
             size_mb=10.0, span=(0.0, 100.0), pos=5.0, delta=+66.0,
             expect="seek")        # 66 s → 7.08 MB < 8 MB
    run_case("target just past the safety margin",
             size_mb=10.0, span=(0.0, 100.0), pos=5.0, delta=+85.0,
             expect="restart")     # 85 s → 9.11 MB > 8 MB

    # 5. The playhead sits behind the origin the old code trusted:
    #    play.log:6382 asked for start_at=14.8 s but the bridge opened on
    #    the keyframe at 10.1 s. The map must come from the file, not
    #    from the request.
    s = run_case("playhead behind requested start (log:6382)",
                 size_mb=6.5, span=(10.1, 1591.0), pos=10.1, delta=+10.0,
                 expect="seek", file_start=14.8)
    check("  origin taken from the file, not the request",
          "origin=10.1" in (s.log[-1] if s.log else ""),
          s.log[-1] if s.log else "")

    # 5b. Pressing back at the very start of a video: the target clamps to
    #     0.0 and the file starts at 0.0, so the fast path applies. This
    #     was a livelock — out of range meant a session restart, whose
    #     replay of the queued press restarted again, every 6.5 s.
    run_case("back at t=0 with the file starting at 0",
             size_mb=4.8, span=(0.0, 34.1), pos=0.0, delta=-10.0,
             expect="seek")

    # 5c. Same press, but the file starts mid-video: 0.0 really is not on
    #     disk, so the restart is the honest answer — and it must not be
    #     mistaken for 5b.
    run_case("back to t=0 when the file starts at 111.8 s",
             size_mb=4.8, span=(111.8, 1145.9), pos=112.0, delta=-10.0,
             expect="restart")

    # 6. A press during a restart is kept as the latest intent and
    #    applied once the session is open.
    s = Stub(int(18.5 * MB), (0.0, 122.0), 4.8)
    try:
        s._restart_lock.acquire()
        s.press(+10.0)
        s.press(+60.0)                       # latest press wins
        check("press during restart is kept, not discarded",
              s._pending_delta_sec == 60.0 and not s.ipc and not s.restarts,
              f"pending={s._pending_delta_sec}")
        s._restart_lock.release()
        bp.LivePlayer._replay_pending_seek(s, s.cprint)
        s.settle()
        check("queued press replayed exactly once",
              s.ipc == ["SEEK_REL 60.0"] and s._pending_delta_sec is None,
              f"ipc={s.ipc} pending={s._pending_delta_sec}")
        bp.LivePlayer._replay_pending_seek(s, s.cprint)
        check("second replay is a no-op",
              s.ipc == ["SEEK_REL 60.0"], f"ipc={s.ipc}")
    finally:
        s.close()

    # 7. SponsorBlock deltas are a distance to a segment end, not a
    #    relative nudge — replaying one from the new position overshoots,
    #    so they must not be queued.
    s = Stub(int(18.5 * MB), (0.0, 122.0), 4.8)
    try:
        s._restart_lock.acquire()
        s.press(+42.0, user_press=False)
        check("sponsor skip is not queued",
              s._pending_delta_sec is None, f"pending={s._pending_delta_sec}")
        s._restart_lock.release()
    finally:
        s.close()

    # 8. Degraded mode: no measurement available → stay conservative
    #    (one wasted restart beats seeking past the writer's edge).
    run_case("no measurement → conservative restart",
             size_mb=18.5, span=None, pos=4.8, delta=+10.0,
             expect="restart")

    # 9. Empty/just-created file must not divide by zero.
    run_case("empty file does not crash",
             size_mb=0.0, span=(0.0, 0.0), pos=0.0, delta=+10.0,
             expect="restart")

    for name, ok in CASES:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    failed = sum(1 for _, ok in CASES if not ok)
    print("\nall passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
