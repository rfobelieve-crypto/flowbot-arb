"""B5 control channel: four verbs, out of band.

The properties under test are the ones LIVE_50U_SPEC.md §6 fixed before any
of this was written: every verb is idempotent, `pause` stops opening without
stopping closing, `flat` verifies real positions BEFORE it closes anything
(the /okx-admin/heal scar), and nothing here can lift a HALT.

Run:  python3 -m pytest tests/  (or  python3 tests/test_control.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from entropy_arb.control import VERBS, ControlChannel, TelegramClient  # noqa: E402
from test_maker import approx, make_engine, run                        # noqa: E402


def chan(eng):
    return ControlChannel(eng, eng.cfg)


def say(eng, verb):
    """Apply one verb the way the channel would, and return its reply."""
    c = chan(eng)
    return run(c._apply(verb.lstrip("/")))


# ------------------------------------------------------------- idempotency

def test_pause_is_idempotent():
    eng = make_engine()
    first = say(eng, "pause")
    assert eng.paused_by_operator and "paused" in first
    second = say(eng, "pause")
    assert eng.paused_by_operator, "the second pause changed the state"
    assert "already paused" in second


def test_resume_is_idempotent():
    eng = make_engine()
    assert "not paused" in say(eng, "resume")
    say(eng, "pause")
    say(eng, "resume")
    assert not eng.paused_by_operator
    assert "not paused" in say(eng, "resume")


def test_flat_is_idempotent():
    eng = make_engine()
    eng.entropy.position = 1.0
    first = say(eng, "flat")
    assert eng.flatten_request and "flattening" in first
    second = say(eng, "flat")
    assert "already flattening" in second
    assert eng.flatten_request


# --------------------------------------------------- what it may NOT undo

def test_resume_cannot_lift_a_halt():
    """A halt is one-way on purpose: the restart is what re-reads real
    positions strictly. A message from a phone does not get to undo it."""
    eng = make_engine()
    eng._risk_halt("test")
    reply = say(eng, "resume")
    assert "REFUSED" in reply and "HALTED" in reply
    assert eng.halted, "a control message cleared a kill switch"


def test_resume_refuses_mid_flatten():
    eng = make_engine()
    eng.entropy.position = 1.0
    say(eng, "flat")
    reply = say(eng, "resume")
    assert "REFUSED" in reply
    assert eng.paused_by_operator, "resumed into a running flatten"


def test_the_channel_has_only_four_verbs():
    assert set(VERBS) == {"pause", "resume", "flat", "status"}


def test_unknown_verbs_do_nothing():
    eng = make_engine()
    c = chan(eng)
    run(c._handle("/size 500", "telegram"))
    run(c._handle("buy me a coffee", "telegram"))
    assert not eng.paused_by_operator and not eng.flatten_request
    assert c.applied == 0


# ------------------------------------------------------ pause semantics

def test_pause_stops_opening():
    eng = make_engine()
    say(eng, "pause")

    async def go():
        await eng._evaluate()
    run(go())
    assert not eng.entropy.sent_makers, "quoted while paused"


def test_pause_does_not_stop_closing():
    """Stopping the opening and the closing at the same time is not
    caution, it is the worst of both."""
    eng = make_engine(max_net_base=1e6)
    say(eng, "pause")
    eng.entropy.position = 1.0
    run(eng._maybe_hedge())
    assert eng.entropy.sent_takers, "the pause blocked a hedge"


def test_pause_pulls_a_resting_quote():
    eng = make_engine(maker_timeout_sec=30.0)

    def hook(eng, m, t, p, o):
        def on_poll(v, n):
            if n == 1:
                eng.set_operator_pause(True, "operator")
        m.on_poll = on_poll
        m.on_cancel = lambda v: setattr(v, "ex_status", "canceled")
    from test_maker import _drive
    p, order = _drive(eng, hook)
    assert eng.entropy.cancels
    assert "paused by operator" in order.stats.get("cancel_reason", "")


# ------------------------------------------------------------ flat verb

def test_flat_closes_both_legs_reduce_only():
    eng = make_engine()
    eng.entropy.position = -0.5
    eng.hedge.position = 0.5
    done = run(eng._flatten_step())
    assert done is False                      # first pass does the closing
    assert eng.entropy.sent_takers and eng.hedge.sent_takers
    approx(eng.entropy.position, 0.0)
    approx(eng.hedge.position, 0.0)
    assert run(eng._flatten_step()) is True   # nothing left to do


def test_flat_will_not_close_blind():
    """A venue whose book is stale, or which is unreachable, is not a venue
    you send a price-protected order to."""
    eng = make_engine()
    eng.entropy.position = 1.0
    eng.entropy.book.alive_ts = 0.0           # stale
    assert run(eng._flatten_step()) is False
    assert not eng.entropy.sent_takers
    eng.entropy.set_book(100.00, 100.20)      # fresh again
    eng._venue_down["entropy"] = 1.0
    assert run(eng._flatten_step()) is False
    assert not eng.entropy.sent_takers


def test_flat_verifies_real_positions_first():
    """The /okx-admin/heal scar: anything that changes real exchange state
    checks the real exchange first."""
    eng = make_engine()
    eng.entropy.position = 0.0                # what we believe
    eng.entropy.chain_position = 1.5          # what is actually there

    async def go():
        # the engine builds its events inside asyncio.run(); rebind so this
        # test's loop owns them too
        eng._flatten_evt = asyncio.Event()
        eng.request_flatten()
        t = asyncio.create_task(eng._flatten_loop())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not eng.flatten_request:
                break
        eng.request_stop()
        eng._flatten_evt.set()
        try:
            await asyncio.wait_for(t, timeout=2)
        except asyncio.TimeoutError:
            t.cancel()
    run(go())
    assert eng.entropy.sent_takers, "nothing was closed"
    _, qty, _ = eng.entropy.sent_takers[0]
    assert abs(qty - 1.5) < 1e-6, \
        f"closed {qty}, i.e. the position we believed rather than the real one"
    assert not eng.flatten_request             # it finished
    assert eng.paused_by_operator, "the pause did not outlive the flatten"


def test_flat_leaves_the_engine_paused():
    eng = make_engine()
    eng.request_flatten()
    assert eng.paused_by_operator and eng.pause_source == "flatten"


# --------------------------------------------------------- the plumbing

def test_command_file_is_consumed_once():
    """`echo flat > control.cmd` over SSH, for the day Telegram is the thing
    that broke. Emptying the file is what stops a stale verb re-firing after
    a restart."""
    eng = make_engine()
    path = os.path.join(tempfile.mkdtemp(prefix="arb-ctl-"), "control.cmd")
    eng.cfg.control_command_file = path
    c = chan(eng)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("pause\n\nstatus\n")
    assert c._drain_command_file() == ["pause", "status"]
    assert c._drain_command_file() == []
    assert open(path, encoding="utf-8").read() == ""


def test_command_file_absent_is_not_an_error():
    eng = make_engine()
    eng.cfg.control_command_file = os.path.join(tempfile.gettempdir(),
                                                "arb-no-such-file.cmd")
    assert chan(eng)._drain_command_file() == []


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *a, **kw):
        return _Resp(self.payload)


def test_only_the_configured_chat_is_obeyed():
    """Someone else finding the bot must not be able to drive it -- and must
    not get an answer that confirms it exists."""
    payload = {"result": [
        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/flat"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/pause"}},
    ]}
    tg = TelegramClient(_Session(payload), "tok", "42")
    msgs, offset = run(tg.poll(0))
    assert msgs == ["/pause"]
    assert offset == 3


def test_critical_logs_are_relayed_not_sent_inline():
    """A logging handler that did network I/O would stall the order path."""
    import logging
    from entropy_arb.control import make_relay
    r = make_relay()
    root = logging.getLogger("relay-test")
    root.addHandler(r)
    root.warning("not critical")
    root.critical("HALTED: something")
    root.removeHandler(r)
    assert len(r.lines) == 1 and "HALTED" in r.lines[0]


def test_status_reports_every_state_flag():
    eng = make_engine()
    eng._risk_halt("test")
    eng.set_operator_pause(True, "operator")
    eng.flatten_request = True
    s = eng.control_status()
    for needle in ("HALTED", "PAUSED", "FLATTENING", "ENTROPY", "net="):
        assert needle in s, needle


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:52s} OK")


# ------------------------------------------- liquidation / foreign trades

def _reconcile(eng, v):
    async def go():
        await eng._reconcile_venue(v, strict=False)
    run(go())


def test_a_position_that_moved_without_us_halts():
    """A liquidation leaves no message. What it does leave is a position that
    moved while this engine sent nothing."""
    import time
    eng = make_engine(max_net_base=1e6)
    eng.entropy.last_traded_ts = time.time() - 3600   # we have been idle
    eng.entropy.position = 1.0
    eng.entropy.chain_position = 0.0                  # it is simply gone
    _reconcile(eng, eng.entropy)
    assert eng.halted, "an unexplained position move did not halt the engine"
    assert eng.unexplained_events == 1
    approx(eng.entropy.position, 0.0)                 # truth is still adopted


def test_our_own_late_fill_is_not_unexplained():
    """An unresolved send returns filled_base 0.0, so the chain learns the
    fill before we do. That delta IS ours and must not read as a liquidation."""
    import time
    eng = make_engine(max_net_base=1e6)
    # past RECONCILE_GRACE_SEC (5s) so the read happens at all, but well
    # inside the attribution window, so the delta is still ours
    eng.entropy.last_traded_ts = time.time() - 8
    eng.entropy.position = 0.0
    eng.entropy.chain_position = 1.0
    _reconcile(eng, eng.entropy)
    assert not eng.halted and eng.unexplained_events == 0
    approx(eng.entropy.position, 1.0)


def test_a_resting_quote_explains_a_position_move():
    """A maker order can fill at any moment; the fill may reach the chain
    before our poll sees it."""
    import time
    from entropy_arb.maker import MakerOrder
    eng = make_engine(max_net_base=1e6)
    eng.entropy.last_traded_ts = time.time() - 3600
    eng._maker_open["entropy"] = MakerOrder(
        venue_key="entropy", is_buy=False, qty=1.0, px=100.19, sent_ts=time.time())
    eng.entropy.chain_position = -1.0
    _reconcile(eng, eng.entropy)
    assert not eng.halted and eng.unexplained_events == 0


def test_startup_read_is_never_unexplained():
    """At startup there is no history to compare against."""
    eng = make_engine(max_net_base=1e6)
    eng.entropy.chain_position = 5.0

    async def go():
        await eng._reconcile_venue(eng.entropy, strict=True)
    run(go())
    assert not eng.halted and eng.unexplained_events == 0
    approx(eng.entropy.position, 5.0)


def test_dust_is_not_a_liquidation():
    import time
    eng = make_engine(max_net_base=1e6)
    eng.entropy.last_traded_ts = time.time() - 3600
    eng.entropy.chain_position = 0.0005        # below net_tolerance_base 0.001
    _reconcile(eng, eng.entropy)
    assert not eng.halted and eng.unexplained_events == 0
