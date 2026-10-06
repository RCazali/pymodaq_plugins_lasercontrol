"""Create the PyMoDAQ dashboard configuration of the office simulator (LaserSim plugins, T1.3).

* PyMoDAQ 5.1: a dashboard **preset** ``<name>.xml`` in the PyMoDAQ preset folder,
* PyMoDAQ 5.2: an **experiment** ``<name>.xml`` in the PyMoDAQ experiment folder.

Content: six simulated motors of the laser (OC_H, OC_V, EM_H, EM_V, JET_X, JET_Y), four motors of the
two test benches (STEER_H / STEER_V: beam steering, PD_X / PD_Y: photodiode) and the 0D viewer
``SimB`` that plays the role of computer B (centroids, sums, laser power, XUV flux, beam position,
photodiode current, frame counter). Random intensity drops of ``SimB`` are off by default (raise
"Intensity drops / min" in its settings to try the intensity guard).

The roles of the simulator are added to the roles file of this machine (``roles_<machine>.toml``,
existing roles are never modified), with the same role names as in the lab example
(``docs/examples/roles_lab.toml``) so that presets made at the office load in the lab.

A Laser Control preset "Simulator demo" is written too: PIDs and optimisations with a button in
the Alignment tab, safety limits, alignment axes, and routines (Routines tab, buttons in Alignment) on the
beam walk + hard aperture bench (coupled near / far field PIDs "Pointing x / y"): HA optimisation with the
PIDs paused, periodic HA optimisation, automated beam walk one mirror at a time.

Usage::

    python -m pymodaq_plugins_lasercontrol.tools.sim_preset            # name 'laser_sim'
    python -m pymodaq_plugins_lasercontrol.tools.sim_preset my_name --force

then open the dashboard with this preset / experiment and the 'Laser Control' extension
(or ``python -m pymodaq_plugins_lasercontrol.extensions.laser_control laser_sim``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

DEFAULT_NAME = 'laser_sim'
DEFAULT_MOVES: Tuple[Tuple[str, str], ...] = (
    ('Sim OC_H', 'OC_H'), ('Sim OC_V', 'OC_V'), ('Sim EM_H', 'EM_H'), ('Sim EM_V', 'EM_V'),
    ('Sim JET_X', 'JET_X'), ('Sim JET_Y', 'JET_Y'),
    ('Sim STEER_H', 'STEER_H'), ('Sim STEER_V', 'STEER_V'), ('Sim PD_X', 'PD_X'), ('Sim PD_Y', 'PD_Y'),
    ('Sim CPL_A', 'CPL_A'), ('Sim CPL_B', 'CPL_B'),
    ('Sim BW1_H', 'BW1_H'), ('Sim BW1_V', 'BW1_V'), ('Sim BW2_H', 'BW2_H'), ('Sim BW2_V', 'BW2_V'),
    ('Sim HA_X', 'HA_X'), ('Sim HA_Y', 'HA_Y'))
DEFAULT_DETECTORS: Tuple[str, ...] = ('SimB',)
PLUGIN = 'LaserSim'
DEMO_PRESET = 'Simulator demo'


def is_pymodaq_52() -> bool:
    """PyMoDAQ >= 5.2 replaced the dashboard presets by experiments."""
    try:
        import pymodaq.utils.managers.experiment.experiment_manager  # noqa: F401
        return True
    except ImportError:
        return False


def _find(param, name: str):
    for child in param.children():
        if child.name() == name:
            return child
        found = _find(child, name)
        if found is not None:
            return found
    return None


def _fix_none_values(param):
    """Parameters created without value would be saved empty and 'changed' to None when the file is
    loaded (the 'Show Config' push button would then open the plugin configuration dialog). Give them
    the default the PyMoDAQ preset editor gives: False for booleans, the first choice for lists."""
    for child in param.children():
        if child.type() != 'group' and child.opts.get('value', None) is None:
            if child.type() in ('bool', 'bool_push', 'led', 'led_push'):
                child.setValue(False)
            elif child.type() == 'list':
                limits = child.opts.get('limits') or []
                values = list(limits.values()) if isinstance(limits, dict) else list(limits)
                if values:
                    child.setValue(values[0])
        _fix_none_values(child)


def _find_last_index(list_children=(), name_prefix='', format_string='02.0f'):
    """Next free index of the children ``<prefix>NN``. PyMoDAQ 5.1's own version skips the first digit of
    the index ('move10' is read as 0), so an 11th actuator gets an existing name ('Already have child
    named move10')."""
    idx = [int(c.name()[len(name_prefix):]) for c in list_children
           if c.name().startswith(name_prefix) and c.name()[len(name_prefix):].isdigit()]
    return f'{(max(idx) + 1) if idx else 0:{format_string}}'


def build_preset_51(name: str, moves: Sequence[Tuple[str, str]], detectors: Iterable[str]):
    """Dashboard preset of PyMoDAQ 5.0 / 5.1 (what the preset editor would produce)."""
    from pymodaq_gui.parameter import Parameter
    import pymodaq.utils.managers.preset_manager_utils as pmu  # registers groupmove / groupdet
    if hasattr(pmu, 'find_last_index'):
        pmu.find_last_index = _find_last_index
    p = Parameter.create(title='Preset', name='Preset', type='group', children=[
        {'title': 'Filename:', 'name': 'filename', 'type': 'str', 'value': name},
        {'title': 'Model Settings:', 'name': 'model_settings', 'type': 'group', 'visible': False, 'children': []},
        {'title': 'Moves:', 'name': 'Moves', 'type': 'groupmove'},
        {'title': 'Detectors:', 'name': 'Detectors', 'type': 'groupdet'}])
    for title, axis in moves:
        p.child('Moves').addNew((PLUGIN,))
        m = p.child('Moves').children()[-1]
        m.child('name').setValue(title)
        m.child('params', 'main_settings', 'module_name').setValue(title)
        _find(m.child('params'), 'axis').setValue(axis)
    for title in detectors:
        p.child('Detectors').addNew(('DAQ0D', PLUGIN))
        d = p.child('Detectors').children()[-1]
        d.child('name').setValue(title)
        module_name = _find(d.child('params'), 'module_name')
        if module_name is not None:
            module_name.setValue(title)
        saver = _find(d.child('params'), 'saver_settings')
        if saver is not None:   # applying these values at load time hits a 5.1 viewer quirk (no h5saver yet)
            saver.remove()
    _fix_none_values(p)
    return p


def build_experiment_52(name: str, moves: Sequence[Tuple[str, str]], detectors: Iterable[str]):
    """Experiment of PyMoDAQ 5.2 (what the experiment manager would produce)."""
    from pymodaq_gui.parameter import Parameter
    from pymodaq.utils.managers.experiment.experiment_manager import ExperimentManager
    from pymodaq.utils.managers.modules.utils import ModuleType
    p = Parameter.create(title='Preset', name='Preset', type='group', children=ExperimentManager.params)
    acts = p.child(ModuleType.Actuator.value)
    for title, axis in moves:
        acts.addNew((PLUGIN,))
        m = acts.children()[-1]
        m.child('name').setValue(title)
        _find(m, 'axis').setValue(axis)
    dets = p.child(ModuleType.Detector.value)
    for title in detectors:
        dets.addNew(('DAQ0D', PLUGIN))
        dets.children()[-1].child('name').setValue(title)
    _fix_none_values(p)
    return p


def target_path(name: str = DEFAULT_NAME) -> Path:
    if is_pymodaq_52():
        from pymodaq.utils.config import get_set_experiment_path
        return Path(get_set_experiment_path()).joinpath(f'{name}.xml')
    from pymodaq.utils.config import get_set_preset_path
    return Path(get_set_preset_path()).joinpath(f'{name}.xml')


def write_sim_config(name: str = DEFAULT_NAME, moves: Sequence[Tuple[str, str]] = DEFAULT_MOVES,
                     detectors: Iterable[str] = DEFAULT_DETECTORS, overwrite: bool = False) -> Path:
    """Write the preset (5.1) or experiment (5.2) file and return its path."""
    from pymodaq_gui.parameter import ioxml
    path = target_path(name)
    if path.exists() and not overwrite:
        raise FileExistsError(f'{path} already exists (use --force to replace it)')
    p = build_experiment_52(name, moves, detectors) if is_pymodaq_52() else build_preset_51(name, moves, detectors)
    ioxml.parameter_to_xml_file(p, path.with_suffix(''), overwrite=True)
    return path


#: roles of the simulator: role -> DAQ_Move title (actuators) or channel key (signals)
SIM_ACTUATOR_ROLES = {'OC_H': ('Sim OC_H', 'Output coupler, horizontal'),
                      'OC_V': ('Sim OC_V', 'Output coupler, vertical'),
                      'EM_H': ('Sim EM_H', 'End mirror, horizontal'),
                      'EM_V': ('Sim EM_V', 'End mirror, vertical'),
                      'Jet_X': ('Sim JET_X', 'Gas jet, X'),
                      'Jet_Y': ('Sim JET_Y', 'Gas jet, Y')}
SIM_SIGNAL_ROLES = {'Cam1_x': 'SimB/Cam1/x', 'Cam1_y': 'SimB/Cam1/y', 'Cam1_sum': 'SimB/Cam1/sum',
                    'Cam2_x': 'SimB/Cam2/x', 'Cam2_y': 'SimB/Cam2/y', 'Cam2_sum': 'SimB/Cam2/sum',
                    'Laser_power': 'SimB/Laser/power', 'XUV_flux': 'SimB/XUV/flux'}
SIM_HEARTBEATS = {'SimB': 'SimB/Stats/frame_counter'}
#: test benches of the simulator only (not in the lab roles)
DEMO_ACTUATOR_ROLES = {'Steer_H': ('Sim STEER_H', 'Test bench: steering mirror, H (+ = beam towards +x)'),
                       'Steer_V': ('Sim STEER_V', 'Test bench: steering mirror, V (+ = beam towards +y)'),
                       'PD_X': ('Sim PD_X', 'Test bench: lens in front of the photodiode, X'),
                       'PD_Y': ('Sim PD_Y', 'Test bench: lens in front of the photodiode, Y'),
                       'Cpl_A': ('Sim CPL_A', 'Coupled mount (simple): screw A, moves x (+1) and y (-0.25)'),
                       'Cpl_B': ('Sim CPL_B', 'Coupled mount (simple): screw B, moves y (+1) and x (+0.35)'),
                       'BW1_H': ('Sim BW1_H', 'Beam walk (strong coupling): mirror 1, H'),
                       'BW1_V': ('Sim BW1_V', 'Beam walk (strong coupling): mirror 1, V'),
                       'BW2_H': ('Sim BW2_H', 'Beam walk (strong coupling): mirror 2, H'),
                       'BW2_V': ('Sim BW2_V', 'Beam walk (strong coupling): mirror 2, V'),
                       'HA_X': ('Sim HA_X', 'Hard aperture, X (also shifts the near field x, 0.2 µm/µm)'),
                       'HA_Y': ('Sim HA_Y', 'Hard aperture, Y (also shifts the near field y, 0.2 µm/µm)')}
DEMO_SIGNAL_ROLES = {'Beam_x': ('SimB/Beam/x', 'µm'), 'Beam_y': ('SimB/Beam/y', 'µm'),
                     'PD_current': ('SimB/PD/current', 'mA'),
                     'Cpl_x': ('SimB/Coupled/x', 'µm'), 'Cpl_y': ('SimB/Coupled/y', 'µm'),
                     'Near_x': ('SimB/Near/x', 'µm'), 'Near_y': ('SimB/Near/y', 'µm'),
                     'Far_x': ('SimB/Far/x', 'µm'), 'Far_y': ('SimB/Far/y', 'µm'),
                     'HA_flux': ('SimB/HA/flux', 'mW')}


def _sim_paths():
    from ..ui.app import machine_name, resolve_paths
    from ..utils import Config
    config = Config()
    machine = machine_name(config)
    return machine, resolve_paths(config, machine)


def write_sim_roles(overwrite: bool = False) -> Optional[Path]:
    """Roles of the simulator in the roles file of this machine. An existing file is completed: the
    missing roles are added, existing ones are never changed (``overwrite`` rewrites the whole file).
    Returns the path, or None when nothing had to be written."""
    from ..core.roles import RoleMap
    machine, paths = _sim_paths()
    path = paths.roles_file
    if path.exists() and not overwrite:
        rm = RoleMap.load(path)
    else:
        rm = RoleMap(machine=machine, description='LaserSim simulator (office)', path=path)
    known = set(rm.all_roles())
    changed = not path.exists() or overwrite
    for role, (module, note) in {**SIM_ACTUATOR_ROLES, **DEMO_ACTUATOR_ROLES}.items():
        if role not in known:
            rm.set_actuator(role, module, 'µm', note)
            changed = True
    for role, channel in SIM_SIGNAL_ROLES.items():
        if role not in known:
            rm.set_signal(role, channel)
            changed = True
    for role, (channel, units) in DEMO_SIGNAL_ROLES.items():
        if role not in known:
            rm.set_signal(role, channel, units)
            changed = True
    for det, ch in SIM_HEARTBEATS.items():
        if det not in rm.heartbeats:
            rm.heartbeats[det] = ch
            changed = True
    return rm.save(path) if changed else None


def _axis(name, h=None, v=None, slow=0.5, fast=5.0, labels=('H', 'V')):
    def ch(role, label):
        return {'actuator': role, 'invert': False, 'mode': 'step', 'slow': slow, 'fast': fast, 'min': -500.0,
                'max': 500.0, 'speed_param': '', 'label': label, 'decimals': 2}
    return {'name': name, 'h': ch(h, labels[0]) if h else None, 'v': ch(v, labels[1]) if v else None}


def coupled_pointing_pids(pid) -> list:
    """Coupled PIDs of the beam walk, one per plane: near field + far field with both mirrors, already
    decoupled (as after "Calibration & decoupling › Apply"): weights = columns of the pseudo-inverse of the
    BEAM_WALK matrix (largest weight ±1), Kp = 0.5 / loop gain. The H ↔ V cross-coupling (0.15) is left to
    the loops; measure and apply again in the PID tab to see the procedure."""
    import numpy as np
    from ..hardware.sim import BEAM_WALK
    out = []
    for plane, (m1, m2) in (('x', ('BW1_H', 'BW2_H')), ('y', ('BW1_V', 'BW2_V'))):
        sigs = (f'Near/{plane}', f'Far/{plane}')
        J = np.array([[BEAM_WALK[s].get(m, 0.0) for m in (m1, m2)] for s in sigs])
        W = np.linalg.pinv(J)
        d = pid(f'Pointing_{plane.upper()}', f'Pointing {plane} (near + far)', f'Near_{plane}', m1, stab=False)
        d['motors'] = [m1, m2]
        d['loops'] = []
        for k, role in enumerate((f'Near_{plane}', f'Far_{plane}')):
            w = W[:, k] / np.max(np.abs(W[:, k]))
            gain = float((J @ w)[k])
            d['loops'].append({'signal': role, 'setpoint': 0.0, 'setpoint_mode': 'fixed', 'sign': '+',
                               'kp': round(0.5 / gain, 4), 'ki': 0.0, 'kd': 0.0, 'deadband': 0.1,
                               'weights': {m1: round(float(w[0]), 4), m2: round(float(w[1]), 4)}})
        out.append(d)
    return out


def demo_routines() -> list:
    """Routines of the simulator (structured programs, execution only): HA check with the PIDs paused,
    "keep aligned" with a periodic HA check, beam transport along positions with a slow ramp, and the
    classic beam walk one mirror at a time."""
    from ..engines.program import Cond, Every, Node, Position, Program
    X, Y = 'PID:Pointing_X', 'PID:Pointing_Y'
    tol = {X: 0.5, Y: 0.8}

    def locked(timeout=60.0, then='next'):
        return Node(until=[Cond('locked', tolerances=dict(tol), hold_s=3.0, outcome=then)], timeout_s=timeout,
                    text='each PID within its tolerance')

    def ha_check():
        return [Node(action='pause', target=X, text='nothing else moves during the check'),
                Node(action='pause', target=Y),
                Node(action='confirm', target='OPT:Optim_HA', on_fail='ask',
                     text='not confirmed: the PIDs hold the beam and the operator decides'),
                Node(action='resume', target=X), Node(action='resume', target=Y), locked(60.0)]

    start = [Node(action='start', target=X, text='near + far field x'), Node(action='start', target=Y),
             locked(120.0)]
    walk_body = [
        Node(action='start', target='PID:BW_Near_X',
             until=[Cond('locked', tolerances={'PID:BW_Near_X': 0.3}, hold_s=2.0),
                    Cond('travel', 'BW1_H', value=15.0)],
             timeout_s=30.0, on_timeout='next', text='near field with mirror 1'),
        Node(action='stop', target='PID:BW_Near_X'),
        Node(action='start', target='PID:BW_Far_X',
             until=[Cond('locked', tolerances={'PID:BW_Far_X': 0.3}, hold_s=2.0), Cond('travel', 'BW2_H', value=15.0)],
             timeout_s=30.0, on_timeout='next', text='far field with mirror 2'),
        Node(action='stop', target='PID:BW_Far_X'),
        Node(text='both centred?', until=[Cond('count', value=8, outcome='fail'),
                                          Cond('signal', 'Near_x', '|x|>', 0.6, outcome='next'),
                                          Cond('signal', 'Far_x', '|x|>', 0.6, outcome='next'),
                                          Cond('delay', value=0.5, outcome='break')])]
    programs = [
        Program('HA_opt', 'HA check (PIDs paused)',
                description='Lock the pointing, pause it, confirm the hard aperture is at its maximum, resume.',
                main=start + ha_check()),
        Program('Keep_aligned', 'Keep aligned (HA every 2 min)',
                description='Lock the pointing and hold it; every 2 minutes the HA is confirmed at its maximum.',
                main=start + [Node(action='hold', text='until stopped')],
                background=[Every(120.0, 'HA check', ha_check())]),
        Program('Transport', 'Beam transport P1 → Final',
                description='Move the beam slowly along positions (setpoints near / far), never leaving it behind; '
                            'the HA is confirmed at its maximum every 30 s, interrupting the ramp at any moment.',
                main=start + [
                    Node(type='foreach', text='positions = setpoints near / far (µm)', positions=[
                        Position('P1', {X: [4.0, 1.0], Y: [2.0, 0.5]}),
                        Position('P2', {X: [6.0, 1.5], Y: [3.0, 0.8]}),
                        Position('Final', {X: [8.0, 2.0], Y: [4.0, 1.0]})], body=[
                        Node(type='ramp', speed=0.2, tolerances=dict(tol), text='waits while a PID lags'),
                        locked(120.0)]),
                    Node(action='hold', text='stay on the final position')],
                background=[Every(30.0, 'HA check', ha_check())]),
        Program('Walk_X', 'Beam walk x, one mirror at a time',
                description='Classic manual beam walk, automated: near field with mirror 1, far field with mirror '
                            '2, again until both are centred (at most 8 times). Compare with "Pointing x".',
                main=[Node(action='stop', target=X, text='the coupled PID uses the same mirrors'),
                      Node(type='repeat', body=walk_body)]),
    ]
    return [p.to_dict() for p in programs]


def demo_preset() -> dict:
    """Laser Control preset of the simulator: what the Alignment buttons need to be tried."""
    from ..presets import default_optimizer, default_pid, default_preset, normalize

    def pid(pid_id, label, signal, motor, show=True, max_step=2.0, kp=0.6, stab=True):
        d = default_pid(pid_id)
        # the motor integrates the steps already: P alone removes the error (Ki > 0: slow unwinding after a big error)
        d.update({'label': label, 'show_in_alignment': show, 'in_stabilization': stab, 'motors': [motor],
                  'loops': [{'signal': signal, 'setpoint': 0.0, 'setpoint_mode': 'fixed', 'sign': '+', 'kp': kp,
                             'ki': 0.0, 'kd': 0.0, 'deadband': 0.1, 'weights': {motor: 1.0}}]})
        d['settings'].update({'max_step': max_step, 'period_s': 0.5})
        return d

    def opt(opt_id, label, motors, signal, action='single', max_step=4.0, probe=2.0, interval=1.0):
        d = default_optimizer(opt_id)
        d.update({'label': label, 'show_in_alignment': True, 'button_action': action,
                  'motors': [{'role': m, 'max_step': max_step, 'probe_step': probe, 'order': i}
                             for i, m in enumerate(motors)],
                  'signals': [{'role': signal, 'mode': 'Maximize', 'weight': 1.0, 'p': 2.0, 'target_value': 0.0,
                               'auto_target': True}]})
        # deadband_scale: the historical default (1.0) ignores changes below 25 % of the signal
        d['config'].update({'interval_min': interval, 'dwell': 0.1, 'max_iterations': 12, 'noise_samples': 4,
                            'probe_samples': 2, 'deadband_scale': 0.05, 'stall_patience': 3})
        return d

    p = default_preset(DEMO_PRESET)
    p['description'] = ('Simulator: "pointing" test bench (Steer_H/V → Beam_x/y, stabilized by PID), '
                        '"photodiode" test bench (PD_X/Y → PD_current, optimized), two coupled benches '
                        '(coupled mount Cpl_A/B → Cpl_x/y, moderate coupling; beam walk BW1/BW2 → Near/Far x/y, '
                        'strong coupling, as independent PIDs and as coupled PIDs "Pointing x / y"), a hard '
                        'aperture on that beam (HA_X/Y → HA_flux) and routines combining them, plus the simulated '
                        'laser (OC, EM, jet).')
    lim = {'Steer_H': (3.0, 40.0, 200.0), 'Steer_V': (3.0, 40.0, 200.0), 'PD_X': (5.0, 40.0, 300.0),
           'PD_Y': (5.0, 40.0, 300.0), 'OC_H': (2.0, 30.0, 150.0), 'OC_V': (2.0, 30.0, 150.0),
           'EM_H': (2.0, 30.0, 150.0), 'EM_V': (2.0, 30.0, 150.0), 'Jet_X': (2.0, 30.0, 150.0),
           'Jet_Y': (2.0, 30.0, 150.0), 'Cpl_A': (3.0, 40.0, 200.0), 'Cpl_B': (3.0, 40.0, 200.0),
           'BW1_H': (3.0, 40.0, 300.0), 'BW1_V': (3.0, 40.0, 300.0), 'BW2_H': (3.0, 40.0, 300.0),
           'BW2_V': (3.0, 40.0, 300.0), 'HA_X': (3.0, 40.0, 300.0), 'HA_Y': (3.0, 40.0, 300.0)}
    p['safety']['actuators'] = {r: {'max_step': a, 'max_excursion': b, 'abs_min': None, 'abs_max': None,
                                    'rate_window_s': 60.0, 'rate_max': c, 'resolution': 0.01}
                                for r, (a, b, c) in lim.items()}
    p['safety']['intensity'].update({'enabled': True, 'role': 'Laser_power', 'mode': 'relative', 'threshold': 0.5,
                                     'hysteresis': 0.1, 'reference': None})
    p['pids'] = [pid('Beam_H', 'Beam H', 'Beam_x', 'Steer_H'),
                 pid('Beam_V', 'Beam V', 'Beam_y', 'Steer_V'),
                 pid('Cam1_X_OC', 'Cam1 X ← OC_H', 'Cam1_x', 'OC_H', max_step=1.5),
                 pid('Cam1_Y_OC', 'Cam1 Y ← OC_V', 'Cam1_y', 'OC_V', show=False, max_step=1.5),
                 # coupled benches driven by independent PIDs: works (simple) / slow, fighting (beam walk)
                 pid('Cpl_X', 'Coupled x', 'Cpl_x', 'Cpl_A', show=False, stab=False),
                 pid('Cpl_Y', 'Coupled y', 'Cpl_y', 'Cpl_B', show=False, stab=False),
                 pid('BW_Near_X', 'BW near x', 'Near_x', 'BW1_H', show=False, stab=False),
                 pid('BW_Far_X', 'BW far x', 'Far_x', 'BW2_H', show=False, kp=0.4, stab=False),
                 pid('BW_Near_Y', 'BW near y', 'Near_y', 'BW1_V', show=False, stab=False),
                 pid('BW_Far_Y', 'BW far y', 'Far_y', 'BW2_V', show=False, kp=0.4, stab=False)]
    p['pids'] += coupled_pointing_pids(pid)
    p['optimizers'] = [opt('Optim_PD', 'Photodiode optim', ['PD_X', 'PD_Y'], 'PD_current'),
                       opt('Track_PD', 'Photodiode tracking', ['PD_X', 'PD_Y'], 'PD_current',
                           action='periodic', interval=0.5),
                       opt('Optim_OC', 'OC power optim', ['OC_H', 'OC_V'], 'Laser_power', max_step=2.0),
                       opt('Optim_HA', 'Hard aperture optim', ['HA_X', 'HA_Y'], 'HA_flux', max_step=5.0, probe=1.5)]
    p['optimizers'][-1]['show_in_alignment'] = False        # driven by the routines
    p['routines'] = demo_routines()
    p['alignment'] = {'axes': [_axis('Pointing', 'Steer_H', 'Steer_V'),
                               _axis('Photodiode', 'PD_X', 'PD_Y', labels=('X', 'Y')),
                               _axis('OC', 'OC_H', 'OC_V', labels=('Horiz', 'Vert')),
                               _axis('End mirror', 'EM_H', 'EM_V', labels=('Horiz', 'Vert')),
                               _axis('Gas Jet xy', 'Jet_X', 'Jet_Y', labels=('X', 'Y')),
                               _axis('Coupled mount', 'Cpl_A', 'Cpl_B', labels=('A', 'B')),
                               _axis('Beam walk M1', 'BW1_H', 'BW1_V'),
                               _axis('Beam walk M2', 'BW2_H', 'BW2_V'),
                               _axis('Hard aperture', 'HA_X', 'HA_Y', labels=('X', 'Y')),
                               _axis('Beam setpoint', 'SP:Beam_H', 'SP:Beam_V', slow=0.2, fast=2.0,
                                     labels=('x', 'y'))],
                      'active': 0, 'hide_unassigned': False}
    p['overview'] = {'plot_roles': ['Beam_x', 'Beam_y', 'PD_current', 'Near_x', 'Far_x', 'HA_flux']}
    return normalize(p)


def write_demo_preset(overwrite: bool = False) -> Optional[Path]:
    """Write "Simulator demo" in the Laser Control presets folder (None if it exists already)."""
    from ..presets import PresetStore
    _machine, paths = _sim_paths()
    store = PresetStore(paths.presets)
    if store.exists(DEMO_PRESET) and not overwrite:
        return None
    return store.save(demo_preset())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Creates the dashboard preset (PyMoDAQ 5.1) or experiment (5.2) '
                                                 'of the LaserSim simulator for office tests.')
    parser.add_argument('name', nargs='?', default=DEFAULT_NAME, help=f'name (default: {DEFAULT_NAME})')
    parser.add_argument('--force', action='store_true',
                        help=f'replace the dashboard preset / experiment and the "{DEMO_PRESET}" preset')
    parser.add_argument('--no-roles', action='store_true', help='do not write the roles file of the simulator')
    args = parser.parse_args(argv)
    from qtpy import QtWidgets
    if QtWidgets.QApplication.instance() is None:
        _app = QtWidgets.QApplication(sys.argv[:1])  # noqa: F841  (Qt objects need an application)
    kind = 'experiment' if is_pymodaq_52() else 'preset'
    try:
        path = write_sim_config(args.name, overwrite=args.force)
        print(f'simulator {kind} written: {path}')
    except FileExistsError as e:
        print(f'{e}\n  (the Steer / PD test benches need the up-to-date file: run again with --force)')
    if not args.no_roles:
        roles = write_sim_roles(overwrite=False)
        print(f'simulator roles added: {roles}' if roles else 'simulator roles already present')
    demo = write_demo_preset(overwrite=args.force)
    print(f'Laser Control preset "{DEMO_PRESET}" written: {demo}' if demo else
          f'Laser Control preset "{DEMO_PRESET}" already present: kept (--force to replace it)')
    print(f"Open the dashboard with '{args.name}', then the 'Laser Control' extension,\n"
          f"or: python -m pymodaq_plugins_lasercontrol.extensions.laser_control {args.name}\n"
          f"then Presets → '{DEMO_PRESET}' → Load, and ▶ Acquisition in the status bar.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
