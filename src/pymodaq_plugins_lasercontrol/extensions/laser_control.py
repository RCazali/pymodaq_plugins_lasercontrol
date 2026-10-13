# -*- coding: utf-8 -*-
"""
Laser Control - PyMoDAQ dashboard extension (one extension for the whole laser, decision D2).

Stabilisation of a mode-locked thin-disk laser with intracavity XUV generation: multi-PID on 0D
signals (camera centroids, power...), periodic drift optimiser, routines, 1D scan, keyboard alignment,
safety supervisor (S1-S12), HDF5 recording, versioned presets, logical roles per machine.

Works with PyMoDAQ 5.1.x (developed on 5.1.10) and 5.2.x.
"""
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from pymodaq_gui import utils as gutils
from pymodaq_utils.logger import set_logger, get_module_name

from pymodaq.extensions.utils import CustomExt

from pymodaq_plugins_lasercontrol.utils import Config as PluginConfig
from pymodaq_plugins_lasercontrol.ui.app import LaserControlApp

logger = set_logger(get_module_name(__file__))
plugin_config = PluginConfig()

EXTENSION_NAME = 'Laser Control'
CLASS_NAME = 'LaserControl'


def _icon(color: str, shape: str = 'circle') -> QtGui.QIcon:
    pix = QtGui.QPixmap(32, 32)
    pix.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor(color))
    p.setPen(QtGui.QPen(QtGui.QColor('white'), 2))
    if shape == 'octagon':
        pts = [QtCore.QPointF(10, 2), QtCore.QPointF(22, 2), QtCore.QPointF(30, 10), QtCore.QPointF(30, 22),
               QtCore.QPointF(22, 30), QtCore.QPointF(10, 30), QtCore.QPointF(2, 22), QtCore.QPointF(2, 10)]
        p.drawPolygon(QtGui.QPolygonF(pts))
    elif shape == 'square':
        p.drawRoundedRect(4, 4, 24, 24, 4, 4)
    else:
        p.drawEllipse(4, 4, 24, 24)
    p.end()
    return QtGui.QIcon(pix)


def _std_icon(name: str) -> QtGui.QIcon:
    style = QtWidgets.QApplication.style()
    sp = getattr(QtWidgets.QStyle, name, None)
    return style.standardIcon(sp) if sp is not None else QtGui.QIcon()


def _dashboard_config_name(dashboard) -> str:
    """Dashboard preset (5.1) or experiment (5.2) name, for the titles and the recordings."""
    if dashboard is None:
        return ''
    f = getattr(dashboard, 'preset_file', None)
    if f:
        return Path(str(f)).stem
    em = getattr(dashboard, 'experiment_manager', None)
    for attr in ('entry', 'current_entry', 'experiment_name'):
        v = getattr(em, attr, None) if em is not None else None
        if v:
            return str(v)
    return ''


class LaserControl(CustomExt):
    params = []
    app = None          # class level: CustomExt.__init__ may call hooks before the app exists (5.2)

    def __init__(self, parent: gutils.DockArea, dashboard, data_folder: Path = None):
        super().__init__(parent, dashboard)
        name = _dashboard_config_name(dashboard)
        self.app = LaserControlApp(modules_manager=self.modules_manager, dockarea=self.dockarea,
                                   window=self.mainwindow, dashboard_preset=name, config=plugin_config,
                                   data_folder=data_folder)
        self.setup_ui()
        self._title = f'Laser Control — {name or "dashboard"} — machine {self.app.machine}'
        if self.mainwindow is not None:
            self.mainwindow.installEventFilter(self)
            self._decorate_window()
            QtCore.QTimer.singleShot(0, self._decorate_window)    # PyMoDAQ 5.2 retitles the window afterwards

    def _decorate_window(self):
        if self.mainwindow is None:
            return
        self.mainwindow.setWindowTitle(self._title)
        if self.mainwindow.width() < 1200:
            self.mainwindow.resize(1400, 900)

    # ---------------------------------------------------------------- CustomApp API (5.1 and 5.2)
    def setup_docks_and_widgets(self):
        self.app.build(self.dockarea)

    def setup_docks(self):        # pymodaq_gui 5.0.x calls setup_docks from setup_ui
        self.setup_docks_and_widgets()

    def setup_menus_and_toolbars(self, menubar=None):
        # PyMoDAQ >= 5.2: dashboard toolbar (show / hide the dashboard, experiment and state menus).
        # DashBoard.load_extension checks its 'show_dashboard' action once the extension is created.
        create = getattr(self, 'create_dashboard_toolbar', None)
        if create is not None and getattr(self.dashboard, 'experiment_manager', None) is not None:
            try:
                create(add_break=False)
            except Exception as e:  # pragma: no cover - never block the extension for a toolbar
                logger.warning(f'dashboard toolbar not created: {e}')

    def setup_menu(self, menubar=None):
        pass

    def setup_actions(self):
        self.add_action('estop', "Emergency stop", _icon('#c0392b', 'octagon'),
                        "Stops every automation, then every motor of the dashboard")
        self.add_action('save_preset', 'Save preset', _std_icon('SP_DialogSaveButton'),
                        'Save the current preset')
        self.add_action('record', 'Recording', _icon('#e74c3c'), 'Start / stop the HDF5 recording',
                        checkable=True)
        self.add_action('refresh_hardware', 'Reload hardware', _std_icon('SP_BrowserReload'),
                        'Re-attach the dashboard modules (after adding / changing modules)')

    def connect_things(self):
        self.connect_action('estop', lambda: self.app.emergency_stop('toolbar'))
        self.connect_action('save_preset', lambda: self.app.save_preset())
        self.connect_action('record', lambda: self.app.toggle_recording(self.is_action_checked('record')))
        self.connect_action('refresh_hardware', self.app.refresh_hardware)
        self.app.ui_timer.timeout.connect(self._sync_actions)

    def _sync_actions(self):
        try:
            act = self.get_action('record')
            if act.isChecked() != self.app.recorder.active:
                act.blockSignals(True)
                act.setChecked(self.app.recorder.active)
                act.blockSignals(False)
        except Exception:
            pass

    # ---------------------------------------------------------------- PyMoDAQ hooks
    def stop(self):
        """Programmatic stop requested by PyMoDAQ: automations stopped (motors are not moved)."""
        if self.app is not None:
            self.app.core.stop_all_automations('stop requested by PyMoDAQ')

    def do_things_after_experiment_set(self, experiment_name: str):
        parent = getattr(super(), 'do_things_after_experiment_set', None)
        if parent is not None:
            parent(experiment_name)
        if getattr(self, 'app', None) is not None:
            self.app.refresh_hardware()

    def eventFilter(self, obj, event):
        if obj is self.mainwindow and event.type() == QtCore.QEvent.Close and self.app is not None \
                and not self.app._closing:
            engaged = self.app.engaged_labels()
            if engaged:
                answer = QtWidgets.QMessageBox.question(
                    self.mainwindow, 'Close Laser Control',
                    'Engaged automations: ' + ', '.join(engaged) +
                    '\n\nThey will be stopped (no motor moves). Close?',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
                if answer != QtWidgets.QMessageBox.Yes:
                    event.ignore()
                    return True
            self.app.shutdown()
        return super().eventFilter(obj, event)

    def _quit_fun(self) -> bool:
        return True       # PyMoDAQ 5.3: CustomExt.quit_fun does nothing unless this hook returns True

    def quit_fun(self) -> bool:
        if self.app is not None:
            self.app.shutdown()
        try:
            super().quit_fun()
        except Exception:
            pass
        if self.mainwindow is not None:
            self.mainwindow.close()
        return True       # 5.3: the "Quit" button of the window closes it only if this is True


def main():
    """Standalone launch for development: ``python -m pymodaq_plugins_lasercontrol.extensions.laser_control
    <dashboard preset or experiment name>``."""
    import sys
    try:
        from pymodaq_gui.qt_utils import mkQApp          # pymodaq_gui 5.2 (applies the PyMoDAQ theme)
    except ImportError:
        from pymodaq_gui.utils.utils import mkQApp       # pymodaq_gui 5.0 / 5.1
    app = mkQApp('Laser Control')
    name = sys.argv[1] if len(sys.argv) > 1 else 'laser_sim'
    try:  # PyMoDAQ 5.2: extensions are designated by the upper-cased class name (ExtensionEnum)
        from pymodaq.dashboard import load_dashboard_with_experiment
        dashboard, extension, win = load_dashboard_with_experiment(name, CLASS_NAME.upper())
    except ImportError:  # PyMoDAQ 5.1
        from pymodaq.utils.gui_utils.loader_utils import load_dashboard_with_preset
        dashboard, extension, win = load_dashboard_with_preset(name, EXTENSION_NAME)
    fn = getattr(app, 'exec', None) or app.exec_
    sys.exit(fn())


if __name__ == '__main__':
    main()
