"""Check, on computer A, that Laser Control receives computer B through LECO, with simulated motors only.

A temporary dashboard preset is written (one LaserSim actuator + a LECODirector viewer ``LECO_B``), the dashboard
and the Laser Control extension are opened on it with every Laser Control file in a temporary folder (the roles,
presets and data of this machine are not touched), the acquisition is started, the channels received from B are
listed, then everything is closed and the temporary preset removed.

The LECO coordinator and the relay of computer B must run first::

    python -m pymodaq_plugins_lasercontrol.tools.leco_lab_check --actor CameraStation_linkcheck --seconds 20

Exit code 0 when the channels of B reached Laser Control with a growing frame counter.

``--watch Cam1/x Cam2/x`` prints every second whether an automation could use these channels (USABLE) or would
hold (HELD and why): to check what happens when B stops a camera, its relay or the network.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

PRESET = 'lc_leco_lab_check'


def _app():
    from qtpy import QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        try:
            from pymodaq_gui.qt_utils import mkQApp
        except ImportError:
            from pymodaq_gui.utils.utils import mkQApp
        app = mkQApp('leco_lab_check')
    return app


def _wait(pred=lambda: False, timeout=1.0):
    app = _app()
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    app.processEvents()
    return pred()


def _print_watch(signals, title: str, keys, elapsed: float):
    """One line per second: state of the detector and, per watched channel, what an automation would see."""
    st = signals.detector_status(title)
    parts = [f'{elapsed:5.1f} s', f'{title} {st.state}' + (f' ({st.reason})' if st.reason else '')]
    for key in keys:
        ch = f'{title}/{key}'
        s = signals.latest(ch)
        ok, why = signals.freshness(ch)
        if s is None:
            parts.append(f'{key}: -')
            continue
        meas = f' frame {s.meas:.0f}' if s.meas is not None else ''
        parts.append(f'{key}={s.value:.4g} seq {s.seq}{meas} -> ' + ('USABLE' if ok else f'HELD: {why}'))
    print(' | '.join(parts), flush=True)


def run(host: str, port: int, actor: str, seconds: float, title: str = 'LECO_B', watch=()) -> int:
    _app()
    from qtpy import QtWidgets
    from pymodaq_gui.utils.dock import DockArea
    from pymodaq.dashboard import DashBoard
    import pymodaq_plugins_lasercontrol.ui.app as app_mod
    from pymodaq_plugins_lasercontrol.tools.sim_preset import is_pymodaq_52, write_sim_config
    if is_pymodaq_52():
        print('only for PyMoDAQ 5.1 dashboard presets')
        return 2
    tmp = Path(tempfile.mkdtemp(prefix='leco_lab_check_'))
    cwd = os.getcwd()
    os.chdir(tmp)                       # the 5.1 dashboard creates its default data folder relative to the cwd
    orig_resolve = app_mod.resolve_paths
    app_mod.resolve_paths = lambda config, machine, override=None: orig_resolve(config, 'leco_check', tmp)
    DashBoard.check_update = lambda self, show=True: False
    path = write_sim_config(PRESET, (('Sim OC_H', 'OC_H'),), (), overwrite=True, leco=(title, host, port, actor))
    dashboard = None
    code = 1
    try:
        win = QtWidgets.QMainWindow()
        area = DockArea()
        win.setCentralWidget(area)
        win.show()
        dashboard = DashBoard(area)
        dashboard.set_preset_mode(path)
        ext = dashboard.load_extension_from_name('Laser Control')
        _wait(timeout=2.0)
        a = ext.app
        a.start_detectors()
        prefix = f'{title}/'
        got = _wait(lambda: any(c.startswith(prefix) for c in a.core.signals.channels()), 20)
        counters = []
        if watch:
            a.core.signals.set_heartbeats({title: f'{prefix}Stats/frame_counter'})
        t0 = time.time()
        while time.time() - t0 < seconds:
            _wait(timeout=1.0)
            if watch:
                _print_watch(a.core.signals, title, watch, time.time() - t0)
            fc = [c for c in a.core.signals.channels() if c.startswith(prefix) and c.endswith('frame_counter')]
            if fc:
                try:
                    counters.append(float(a.core.signals.value(fc[0])))
                except Exception:
                    counters.append(float('nan'))
        chans = sorted(c for c in a.core.signals.channels() if c.startswith(prefix))
        print(f'channels of B in Laser Control ({len(chans)}): {", ".join(chans) if chans else "-"}')
        print(f'frame counter seen by Laser Control: {counters[:2]} ... {counters[-2:]}')
        growing = len(counters) >= 2 and counters[-1] > counters[0]
        code = 0 if got and growing else 1
        print('RESULT:', 'OK' if code == 0 else 'FAILED')
        try:
            a.core.broker.stop_grab()
        except Exception:
            pass
        _wait(timeout=0.5)
    finally:
        if dashboard is not None:
            dashboard.quit_fun()
            _wait(timeout=1.0)
        os.chdir(cwd)
        app_mod.resolve_paths = orig_resolve
        for p in path.parent.glob(f'{PRESET}*'):
            try:
                p.unlink()
            except OSError:
                pass
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--host', default='localhost', help='address of the LECO coordinator')
    parser.add_argument('--port', type=int, default=12300)
    parser.add_argument('--actor', required=True, help='LECO name of the relay of computer B')
    parser.add_argument('--seconds', type=float, default=20.0)
    parser.add_argument('--watch', nargs='*', default=[], metavar='CHANNEL',
                        help='channels of B to follow every second (e.g. Cam1/x Cam2/x), with the heartbeat '
                             'Stats/frame_counter declared: what an automation would do with them (USABLE / HELD)')
    a = parser.parse_args(argv)
    return run(a.host, a.port, a.actor, a.seconds, watch=a.watch)


if __name__ == '__main__':
    try:
        code = main()
    finally:
        sys.stdout.flush()
    os._exit(code)
