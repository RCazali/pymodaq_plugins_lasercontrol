"""The routines of the "Simulator demo" preset on the simulator model (beam walk + HA stage), offline: the core
drives fake motors mirrored into the model, the model frames feed the core, time is a fake clock."""
import pytest

from pymodaq_plugins_lasercontrol.core import FakeClock, LaserCore, RoleMap, SafetyConfig, AutoState
from pymodaq_plugins_lasercontrol.core.fakes import FakeActuator, FakeDetector
from pymodaq_plugins_lasercontrol.engines import (OptimizerAutomation, PIDAutomation, Program, ProgramAutomation,
                                                  RoutineHost, validate_program)
from pymodaq_plugins_lasercontrol.hardware.sim import LaserSimModel
from pymodaq_plugins_lasercontrol.tools import sim_preset


class SimBench:
    def __init__(self, seed=3):
        self.clock = FakeClock()
        self.model = LaserSimModel(seed=seed, clock=self.clock.now)
        self.model.dropout_rate_per_min = 0.0
        self.preset = sim_preset.demo_preset()
        roles = RoleMap(machine='sim')
        for role, (module, _) in {**sim_preset.SIM_ACTUATOR_ROLES, **sim_preset.DEMO_ACTUATOR_ROLES}.items():
            roles.set_actuator(role, module, 'µm')
        for role, ch in sim_preset.SIM_SIGNAL_ROLES.items():
            roles.set_signal(role, ch)
        for role, (ch, units) in sim_preset.DEMO_SIGNAL_ROLES.items():
            roles.set_signal(role, ch, units)
        safety = SafetyConfig.from_dict(self.preset['safety'])
        safety.intensity.enabled = False
        self.core = LaserCore(self.clock, roles, safety)
        self.axis = dict(sim_preset.DEFAULT_MOVES)                  # module -> model axis
        self.acts = {m: FakeActuator(m, 0.0) for m in self.axis}
        self.det = FakeDetector('SimB')
        self.core.broker.attach(self.acts, {'SimB': self.det})
        for cfg in self.preset['pids']:
            self.core.add_automation(PIDAutomation.from_config(cfg))
        for o in self.preset['optimizers']:
            opt = OptimizerAutomation(o['config'], o['motors'], o['signals'], auto_id=f"OPT:{o['id']}",
                                      label=o['label'])
            opt.rng.seed(seed)                                    # reproducible probe directions
            self.core.add_automation(opt)
        self.host = RoutineHost(self.core)
        self.step()

    def routine(self, rid):
        cfg = next(r for r in self.preset['routines'] if r['id'] == rid)
        r = ProgramAutomation(Program.from_dict(cfg), self.host)
        self.core.add_automation(r)
        return r

    def value(self, role):
        return self.core.signals.value(self.core.roles.channel_for(role))

    def step(self, dt=0.1):
        self.clock.advance(dt)
        for module, act in self.acts.items():
            if act.moving:
                act.finish()
            self.model.move_to(self.axis[module], act.position(), speed=1e9)
        self.clock.advance(1e-6)
        frame = self.model.frame()
        self.det.emit({f'SimB/{d}/{k}': v for d, row in frame.items() for k, v in row.items()})
        self.core.tick()

    def run(self, seconds, until=None, spy=None):
        for _ in range(int(seconds / 0.1)):
            self.step()
            if spy is not None:
                spy()
            if until is not None and until():
                return True
        return False


def test_ha_check_with_the_pids_paused():
    """The hard aperture is confirmed at its maximum (it climbs back first) with the coupled near / far PIDs
    paused, then they re-lock."""
    bench = SimBench()
    top = bench.model.optimum(bench.clock.now())
    bench.acts['Sim HA_X'].external_move(top['HA_X'] + 3.0)            # aperture a bit off its top
    bench.acts['Sim HA_Y'].external_move(top['HA_Y'])
    bench.step()
    r = bench.routine('HA_opt')
    assert bench.core.start(r.id)[0], bench.core.validate(r)
    groups = [bench.core.get('PID:Pointing_X'), bench.core.get('PID:Pointing_Y')]
    opt = bench.core.get('OPT:Optim_HA')
    seen, end = [], []

    def spy():
        if opt.state == AutoState.ACTIVE and opt.in_pass:
            seen.append(tuple(g.state for g in groups))
        if opt.last_result and not end:                                # off the top at the end of the check
            end.append(bench.core.position('HA_X') - bench.model.optimum(bench.clock.now())['HA_X'])

    assert bench.run(900, until=lambda: r.state == AutoState.OFF, spy=spy)
    assert r.outcome == 'done', (r.reason, list(r.history)[-5:])
    assert r.question is None and opt.last_result['confirmed'], opt.last_result
    assert seen and all(s == (AutoState.PAUSED, AutoState.PAUSED) for s in seen)     # paused during the pass
    assert all(g.state == AutoState.ACTIVE for g in groups)                         # resumed and locked again
    for role in ('Near_x', 'Far_x', 'Near_y', 'Far_y'):
        assert abs(bench.value(role)) < 1.0, role
    assert end and abs(end[0]) < 2.0, end
    assert opt.last_result['motors']['HA_X']['tries'] >= 2               # it climbed back first


def test_beam_walk_one_mirror_at_a_time():
    bench = SimBench(seed=5)
    r = bench.routine('Walk_X')
    assert bench.core.start(r.id)[0]
    assert bench.run(900, until=lambda: r.state == AutoState.OFF)
    assert r.outcome == 'done', (r.reason, list(r.history)[-8:])
    loop = r.program.main[1]
    assert loop.type == 'repeat' and r.visits[loop.body[0].uid] >= 2      # walked more than once
    assert abs(bench.value('Near_x')) < 1.0 and abs(bench.value('Far_x')) < 1.0
    assert bench.core.get('PID:BW_Near_X').state == AutoState.OFF
    assert bench.core.get('PID:BW_Far_X').state == AutoState.OFF


def test_coupled_pid_real_decoupling_on_the_simulator():
    """The PID-tab procedure, end to end on the simulated beam walk: a naive coupled PID (one mirror per
    signal) is measured (every mirror on near and far field), the proposal is applied, and the decoupled PID
    moves each signal alone and converges faster than the naive one."""
    import copy
    import numpy as np
    from pymodaq_plugins_lasercontrol.engines import CalibrationAutomation
    from pymodaq_plugins_lasercontrol.engines.decoupling import decoupling_proposal
    from pymodaq_plugins_lasercontrol.hardware.sim import BEAM_WALK
    from pymodaq_plugins_lasercontrol.presets import normalize_pid
    bench = SimBench(seed=7)
    naive = normalize_pid({'id': 'NFX', 'motors': ['BW1_H', 'BW2_H'], 'settings': {'max_step': 2.0, 'period_s': 0.5},
                           'loops': [{'signal': 'Near_x', 'kp': 0.5, 'deadband': 0.1, 'weights': {'BW1_H': 1.0}},
                                     {'signal': 'Far_x', 'kp': 0.33, 'deadband': 0.1, 'weights': {'BW2_H': 1.0}}]})
    cal = CalibrationAutomation('Near_x', naive['motors'], cycles=3, extra_signals=['Far_x'], settle_s=0.0,
                                period_s=0.5, auto_id='CAL:NFX')
    bench.core.add_automation(cal)
    ok, msg = bench.core.start(cal.id)
    assert ok, msg
    assert bench.run(600, until=lambda: cal.state == AutoState.OFF)
    assert cal.completed, cal.reason
    prop, res, problems = decoupling_proposal(cal.results_by, ['Near_x', 'Far_x'], naive['motors'], 0.25)
    assert prop is not None, problems
    J = np.array([[BEAM_WALK[f'{s}/x'].get(m, 0.0) for m in ('BW1_H', 'BW2_H')] for s in ('Near', 'Far')])
    assert np.allclose(res.J, J, atol=0.1), res.J                         # the measured matrix is the true one
    decoupled = copy.deepcopy(naive)
    for loop in decoupled['loops']:
        p = prop[loop['signal']]
        loop.update(weights=dict(p['weights']), sign=p['sign_kp'], kp=p['kp'], deadband=p['deadband'])
    for loop in decoupled['loops']:                                       # moves only its own signal
        w = np.array([loop['weights'][m] for m in ('BW1_H', 'BW2_H')])
        resp = J @ w
        k = 0 if loop['signal'] == 'Near_x' else 1
        assert abs(resp[1 - k]) < 0.1 * abs(resp[k]), (loop['signal'], resp)

    def iterations_to_lock(cfg):
        b = SimBench(seed=11)
        b.model.drift_amplitude = 0.0
        b.model.random_walk = 0.0
        b.acts['Sim BW1_H'].external_move(6.0)                            # beam off in near and far field
        b.acts['Sim BW2_H'].external_move(-5.0)
        b.step()
        b.core.remove_automation('PID:NFX')
        a = b.core.add_automation(PIDAutomation.from_config(cfg))
        assert b.core.start(a.id)[0]
        ok = b.run(300, until=lambda: abs(b.value('Near_x')) < 0.4 and abs(b.value('Far_x')) < 0.4
                   and a.iterations > 2)
        return a.iterations if ok else 10 ** 6
    n_dec, n_naive = iterations_to_lock(decoupled), iterations_to_lock(naive)
    assert n_dec < 30 and n_dec < n_naive, (n_dec, n_naive)


def test_keep_aligned_loops_until_stopped():
    bench = SimBench(seed=4)
    r = bench.routine('Keep_aligned')
    assert bench.core.start(r.id)[0]
    opt = bench.core.get('OPT:Optim_HA')
    assert not bench.run(560, until=lambda: r.state == AutoState.OFF)     # never ends by itself
    assert r.state == AutoState.ACTIVE and r.interruptions >= 3           # every 2 min: the HA check
    confirm = next(n for n in r.program.background[0].body if n.action == 'confirm')
    assert r.visits[confirm.uid] >= 3 and opt.pass_count >= 3
    assert r.main.node.action == 'hold'                                    # the main sequence holds the beam
    bench.core.stop(r.id, 'user')
    assert r.outcome == 'stopped'
    assert all(bench.core.get(p).state == AutoState.OFF for p in ('PID:Pointing_X', 'PID:Pointing_Y'))


def test_demo_routines_are_valid():
    for cfg in sim_preset.demo_routines():
        assert 'steps' not in cfg and validate_program(Program.from_dict(cfg)) == [], cfg['id']
    bench = SimBench()
    assert [r['id'] for r in bench.preset['routines']] == ['HA_opt', 'Keep_aligned', 'Transport', 'Walk_X']
    for cfg in bench.preset['routines']:
        r = ProgramAutomation(Program.from_dict(cfg), bench.host)
        errs = [e for e in r.validate(bench.core)]
        assert errs == [], (cfg['id'], errs)
