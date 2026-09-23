# -*- coding: utf-8 -*-
"""
Laser Control - PyMoDAQ dashboard extension (one extension for the whole laser, decision D2).

Stabilisation of a mode-locked thin-disk laser with intracavity XUV generation: multi-PID on 0D
signals (camera centroids, power...), periodic drift optimiser, 1D / XY scans, keyboard alignment,
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

    def __init__(self, parent: gutils.DockArea, dashboard):
        super().__init__(parent, dashboard)
        name = _dashboard_config_name(dashboard)
        self.app = LaserControlApp(modules_manager=self.modules_manager, dockarea=self.dockarea,
                                   window=self.mainwindow, dashboard_preset=name, config=plugin_config)
        self.setup_ui()
        if self.mainwindow is not None:
            self.mainwindow.setWindowTitle(f'Laser Control — {name or "dashboard"} — machine {self.app.machine}')
            self.mainwindow.installEventFilter(self)
            if self.mainwindow.width() < 1200:
                self.mainwindow.resize(1400, 900)

    # ---------------------------------------------------------------- CustomApp API (5.1 and 5.2)
    def setup_docks_and_widgets(self):
        self.app.build(self.dockarea)

    def setup_docks(self):        # pymodaq_gui 5.0.x calls setup_docks from setup_ui
        self.setup_docks_and_widgets()

    def setup_menus_and_toolbars(self, menubar=None):
        pass

    def setup_menu(self, menubar=None):
        pass

    def setup_actions(self):
        self.add_action('estop', "Arrêt d'urgence", _icon('#c0392b', 'octagon'),
                        "Arrête tous les automatismes puis tous les moteurs du dashboard")
        self.add_action('save_preset', 'Enregistrer le preset', _std_icon('SP_DialogSaveButton'),
                        'Enregistrer le preset courant')
        self.add_action('record', 'Enregistrement', _icon('#e74c3c'), 'Démarrer / arrêter l’enregistrement HDF5',
                        checkable=True)
        self.add_action('refresh_hardware', 'Relire le matériel', _std_icon('SP_BrowserReload'),
                        'Relier à nouveau les modules du dashboard (après ajout / changement de modules)')

    def connect_things(self):
        self.connect_action('estop', lambda: self.app.emergency_stop('barre d’outils'))
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
            self.app.core.stop_all_automations('arrêt demandé par PyMoDAQ')

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
                    self.mainwindow, 'Fermer Laser Control',
                    'Automatismes engagés : ' + ', '.join(engaged) +
                    '\n\nIls seront arrêtés (aucun moteur ne bouge). Fermer ?',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
                if answer != QtWidgets.QMessageBox.Yes:
                    event.ignore()
                    return True
            self.app.shutdown()
        return super().eventFilter(obj, event)

    def quit_fun(self):
        if self.app is not None:
            self.app.shutdown()
        try:
            super().quit_fun()
        except Exception:
            pass


def main():
    """Standalone launch for development: ``python -m pymodaq_plugins_lasercontrol.extensions.laser_control
    <dashboard preset or experiment name>``."""
    import sys
    from pymodaq_gui.utils.utils import mkQApp
    app = mkQApp('Laser Control')
    name = sys.argv[1] if len(sys.argv) > 1 else 'laser_sim'
    try:  # PyMoDAQ 5.2
        from pymodaq.dashboard import load_dashboard_with_experiment
        dashboard, extension, win = load_dashboard_with_experiment(name, EXTENSION_NAME)
    except ImportError:  # PyMoDAQ 5.1
        from pymodaq.utils.gui_utils.loader_utils import load_dashboard_with_preset
        dashboard, extension, win = load_dashboard_with_preset(name, EXTENSION_NAME)
    fn = getattr(app, 'exec', None) or app.exec_
    sys.exit(fn())


if __name__ == '__main__':
    main()
