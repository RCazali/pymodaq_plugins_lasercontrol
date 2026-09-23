"""Create the PyMoDAQ dashboard configuration of the office simulator (LaserSim plugins, T1.3).

* PyMoDAQ 5.1: a dashboard **preset** ``<name>.xml`` in the PyMoDAQ preset folder,
* PyMoDAQ 5.2: an **experiment** ``<name>.xml`` in the PyMoDAQ experiment folder.

Content: six simulated motors (OC_H, OC_V, EM_H, EM_V, JET_X, JET_Y) and the 0D viewer ``SimB``
that plays the role of computer B (centroids, sums, laser power, XUV flux, frame counter).

When this machine has no roles file yet, the roles of the simulator are written too
(``roles_<machine>.toml``), with the same role names as in the lab example
(``docs/exemples/roles_lab.toml``) so that presets made at the office load in the lab.

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
    ('Sim JET_X', 'JET_X'), ('Sim JET_Y', 'JET_Y'))
DEFAULT_DETECTORS: Tuple[str, ...] = ('SimB',)
PLUGIN = 'LaserSim'


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


def build_preset_51(name: str, moves: Sequence[Tuple[str, str]], detectors: Iterable[str]):
    """Dashboard preset of PyMoDAQ 5.0 / 5.1 (what the preset editor would produce)."""
    from pymodaq_gui.parameter import Parameter
    import pymodaq.utils.managers.preset_manager_utils  # noqa: F401  (registers groupmove / groupdet)
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
        raise FileExistsError(f'{path} existe déjà (utiliser --force pour le remplacer)')
    p = build_experiment_52(name, moves, detectors) if is_pymodaq_52() else build_preset_51(name, moves, detectors)
    ioxml.parameter_to_xml_file(p, path.with_suffix(''), overwrite=True)
    return path


#: roles of the simulator: role -> DAQ_Move title (actuators) or channel key (signals)
SIM_ACTUATOR_ROLES = {'OC_H': ('Sim OC_H', 'Coupleur de sortie, horizontal'),
                      'OC_V': ('Sim OC_V', 'Coupleur de sortie, vertical'),
                      'EM_H': ('Sim EM_H', 'Miroir de fond, horizontal'),
                      'EM_V': ('Sim EM_V', 'Miroir de fond, vertical'),
                      'Jet_X': ('Sim JET_X', 'Jet de gaz, X'),
                      'Jet_Y': ('Sim JET_Y', 'Jet de gaz, Y')}
SIM_SIGNAL_ROLES = {'Cam1_x': 'SimB/Cam1/x', 'Cam1_y': 'SimB/Cam1/y', 'Cam1_sum': 'SimB/Cam1/sum',
                    'Cam2_x': 'SimB/Cam2/x', 'Cam2_y': 'SimB/Cam2/y', 'Cam2_sum': 'SimB/Cam2/sum',
                    'Laser_power': 'SimB/Laser/power', 'XUV_flux': 'SimB/XUV/flux'}
SIM_HEARTBEATS = {'SimB': 'SimB/Stats/frame_counter'}


def write_sim_roles(overwrite: bool = False) -> Optional[Path]:
    """Roles file of this machine for the simulator (None if one already exists and not overwrite)."""
    from ..core.roles import RoleMap
    from ..ui.app import machine_name, resolve_paths
    from ..utils import Config
    config = Config()
    machine = machine_name(config)
    path = resolve_paths(config, machine).roles_file
    if path.exists() and not overwrite:
        return None
    rm = RoleMap(machine=machine, description='Simulateur LaserSim (bureau)', path=path)
    for role, (module, note) in SIM_ACTUATOR_ROLES.items():
        rm.set_actuator(role, module, 'µm', note)
    for role, channel in SIM_SIGNAL_ROLES.items():
        rm.set_signal(role, channel)
    rm.heartbeats.update(SIM_HEARTBEATS)
    return rm.save(path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Crée le preset (PyMoDAQ 5.1) ou l’expérience (5.2) du '
                                                 'simulateur LaserSim pour les essais au bureau.')
    parser.add_argument('name', nargs='?', default=DEFAULT_NAME, help=f'nom (défaut : {DEFAULT_NAME})')
    parser.add_argument('--force', action='store_true', help='remplacer un fichier existant')
    parser.add_argument('--no-roles', action='store_true', help='ne pas écrire le fichier de rôles du simulateur')
    args = parser.parse_args(argv)
    from qtpy import QtWidgets
    if QtWidgets.QApplication.instance() is None:
        _app = QtWidgets.QApplication(sys.argv[:1])  # noqa: F841  (Qt objects need an application)
    try:
        path = write_sim_config(args.name, overwrite=args.force)
    except FileExistsError as e:
        print(e)
        return 1
    kind = 'expérience' if is_pymodaq_52() else 'preset'
    print(f'{kind} du simulateur écrit : {path}')
    if not args.no_roles:
        roles = write_sim_roles(overwrite=args.force)
        print(f'rôles du simulateur écrits : {roles}' if roles else
              'fichier de rôles déjà présent pour cette machine : conservé (--force pour le remplacer)')
    print(f"Ouvrir le dashboard avec « {args.name} » puis l’extension « Laser Control »,\n"
          f"ou : python -m pymodaq_plugins_lasercontrol.extensions.laser_control {args.name}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
