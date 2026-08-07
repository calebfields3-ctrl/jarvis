"""The reactor's animation, tested without a display.

tkinter isn't installed everywhere Jarvis is developed, and there is no
window on CI. All the arithmetic lives in ReactorState precisely so it can
be checked here; the drawing code is a thin layer over it.
"""

from __future__ import annotations

import math
import types

import pytest

from jarvis.gui.hud import (
    BACKDROP,
    MODE_STYLE,
    RING_COUNT,
    HUD,
    Mode,
    ReactorState,
    blend,
    hud_problem,
)


@pytest.fixture
def reactor():
    return ReactorState()


# ---------------------------------------------------------------- colours


def test_blend_produces_a_colour_tkinter_will_accept(reactor):
    for level in (0.0, 0.25, 0.5, 1.0):
        colour = blend((0x22, 0xD3, 0xEE), level)
        assert len(colour) == 7 and colour.startswith("#")
        int(colour[1:], 16)


def test_blend_at_zero_is_the_backdrop_and_at_one_is_the_colour():
    assert blend((0x22, 0xD3, 0xEE), 0.0) == BACKDROP
    assert blend((0x22, 0xD3, 0xEE), 1.0) == "#22d3ee"


def test_blend_clamps_instead_of_raising_mid_frame():
    """A pulse that overshoots should saturate, not crash the render loop."""
    assert blend((0x22, 0xD3, 0xEE), 5.0) == "#22d3ee"
    assert blend((0x22, 0xD3, 0xEE), -3.0) == BACKDROP


# ------------------------------------------------------------------ modes


def test_every_mode_has_a_style(reactor):
    for mode in Mode:
        assert mode in MODE_STYLE


def test_listening_is_visibly_brighter_than_idle(reactor):
    """The whole point is answering 'is it hearing me' without words."""
    idle = ReactorState(mode=Mode.IDLE).ring_colour(0)
    listening = ReactorState(mode=Mode.LISTENING).ring_colour(0)
    assert int(listening[1:3], 16) + int(listening[3:5], 16) > \
           int(idle[1:3], 16) + int(idle[3:5], 16)


def test_listening_spins_faster_than_idle():
    assert MODE_STYLE[Mode.LISTENING][0] > MODE_STYLE[Mode.IDLE][0]


def test_waiting_on_approval_is_not_cyan(reactor):
    """Everything else is cyan, so the one thing needing Caleb has to differ."""
    assert MODE_STYLE[Mode.ASKING][3] != MODE_STYLE[Mode.THINKING][3]


def test_changing_mode_surges_the_reactor(reactor):
    reactor.set_mode(Mode.LISTENING)
    assert reactor.energy > 0


def test_setting_the_same_mode_twice_does_not_re_surge(reactor):
    reactor.set_mode(Mode.LISTENING)
    reactor.tick(0.5)
    settled = reactor.energy
    reactor.set_mode(Mode.LISTENING)
    assert reactor.energy == settled


# ------------------------------------------------------------------- tick


def test_phase_advances_and_wraps(reactor):
    reactor.set_mode(Mode.LISTENING)
    reactor.tick(1.0)
    assert 0.0 <= reactor.phase < 1.0
    for _ in range(100):
        reactor.tick(0.5)
        assert 0.0 <= reactor.phase < 1.0


def test_energy_decays_toward_rest(reactor):
    reactor.excite(1.0)
    for _ in range(200):
        reactor.tick(0.05)
    assert reactor.energy < 0.01


def test_energy_decay_does_not_depend_on_the_frame_rate():
    """A slow frame should not produce a visible lurch."""
    smooth = ReactorState(energy=1.0)
    for _ in range(100):
        smooth.tick(0.01)
    chunky = ReactorState(energy=1.0)
    for _ in range(10):
        chunky.tick(0.1)
    assert smooth.energy == pytest.approx(chunky.energy, rel=1e-6)


def test_energy_never_exceeds_full(reactor):
    for _ in range(50):
        reactor.excite(1.0)
    assert reactor.energy <= 1.0


# --------------------------------------------------------------- geometry


def signed_step(reactor, index, phase=0.1, step=0.001):
    """Which way a ring is travelling, as degrees per unit of phase."""
    reactor.phase = phase
    before = reactor.ring_angle(index)
    reactor.phase = phase + step
    after = reactor.ring_angle(index)
    delta = (after - before + 180) % 360 - 180  # shortest way round
    return delta


def test_rings_turn_in_alternating_directions(reactor):
    """Four concentric circles all spinning together read as one solid disc."""
    directions = [signed_step(reactor, i) > 0 for i in range(RING_COUNT)]
    assert directions == [True, False, True, False]


def test_outer_rings_turn_faster_than_inner_ones(reactor):
    speeds = [abs(signed_step(reactor, i)) for i in range(RING_COUNT)]
    assert speeds == sorted(speeds), "the rings should not move as one piece"


def test_ring_angles_stay_in_range(reactor):
    for phase in (0.0, 0.3, 0.7, 0.999):
        reactor.phase = phase
        for index in range(RING_COUNT):
            assert 0.0 <= reactor.ring_angle(index) < 360.0


def test_ring_radii_stay_positive_and_near_their_base(reactor):
    """A radius that crosses zero inverts the arc and looks like a glitch."""
    for mode in Mode:
        reactor.mode = mode
        for energy in (0.0, 0.5, 1.0):
            reactor.energy = energy
            for pulse in (0.0, 0.25, 0.5, 0.75):
                reactor.pulse = pulse
                for index in range(RING_COUNT):
                    radius = reactor.ring_radius(index, 100.0)
                    assert 60.0 < radius < 140.0


def test_the_reactor_breathes_from_the_edge(reactor):
    """Outer rings should move more than inner ones, or it swells like a balloon."""
    reactor.mode = Mode.SPEAKING
    reactor.energy = 1.0
    swings = []
    for index in range(RING_COUNT):
        radii = []
        for step in range(24):
            reactor.pulse = step / 24
            radii.append(reactor.ring_radius(index, 100.0))
        swings.append(max(radii) - min(radii))
    assert swings[-1] > swings[0]


def test_the_core_grows_with_energy(reactor):
    quiet = reactor.core_radius(26)
    reactor.excite(1.0)
    assert reactor.core_radius(26) > quiet


def test_all_colours_are_valid_in_every_state(reactor):
    for mode in Mode:
        reactor.mode = mode
        for energy in (0.0, 0.5, 1.0):
            reactor.energy = energy
            int(reactor.core_colour()[1:], 16)
            for index in range(RING_COUNT):
                int(reactor.ring_colour(index)[1:], 16)


def test_the_sweep_only_ever_turns_one_way(reactor):
    previous, wraps = -1.0, 0
    for step in range(40):
        reactor.phase = step / 40
        angle = reactor.sweep_angle()
        if angle < previous:
            wraps += 1
        previous = angle
    assert wraps <= 2, "the scan line should sweep, not jitter"


# -------------------------------------------------------- the window shell


def test_updates_are_queued_rather_than_applied_on_the_calling_thread():
    """Touching a widget from the voice thread is a once-a-week crash."""
    hud = HUD()
    hud.set_mode(Mode.LISTENING)
    hud.say("Morning, sir.")
    assert hud.state.mode is Mode.IDLE, "the update was applied off the tk thread"
    assert hud._events.qsize() == 2


def test_draining_applies_what_was_queued():
    hud = HUD()
    hud.set_mode(Mode.THINKING)
    hud.status("scanning")
    hud._drain()
    assert hud.state.mode is Mode.THINKING
    assert hud._status_text == "scanning"


def test_a_malformed_update_does_not_kill_the_render_loop():
    hud = HUD()
    hud._events.put(("line", "not a tuple of two"))
    hud._events.put(("mode", Mode.SPEAKING))
    hud._drain()
    assert hud.state.mode is Mode.SPEAKING, "one bad update took the window down"


def test_a_tool_call_excites_the_reactor():
    hud = HUD()
    call = types.SimpleNamespace(name="read_file", summary="notes.txt", ok=True)
    hud.tool_call(call)
    hud._drain()
    assert hud.state.energy > 0


def test_closing_is_visible_to_other_threads():
    hud = HUD()
    assert not hud.closed
    hud._handle_close()
    assert hud.closed


def test_the_close_hook_runs():
    fired = []
    hud = HUD(on_close=lambda: fired.append(True))
    hud._handle_close()
    assert fired == [True]


def test_a_broken_close_hook_still_closes():
    def explode():
        raise RuntimeError("nope")

    hud = HUD(on_close=explode)
    hud._handle_close()
    assert hud.closed


def test_a_missing_toolkit_is_explained_with_the_fix():
    problem = hud_problem()
    if problem is not None:
        assert "apt install" in problem or "display" in problem
