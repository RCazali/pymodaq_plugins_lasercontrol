"""Simulated 0D data of computer B (centroids, power, XUV flux, frame counter), specification T1.3.

Data names / labels: ``Cam1/{x,y,sum}``, ``Cam2/{x,y,sum}``, ``Laser/power``, ``XUV/flux``,
``Beam/{x,y}`` and ``PD/current`` (test benches: beam steering, photodiode),
``Stats/frame_counter`` (heartbeat, option O9-A). Channel keys seen by Laser Control are
``<viewer title>/<name>/<label>``, e.g. ``SimB/Cam1/x``.

Fault injection from the settings: *Mute* (computer B silent: no frame at all) and *Freeze*
(frames keep arriving but values and frame counter are frozen), noise, drift, intensity drops.
"""
import numpy as np
from qtpy import QtCore

from pymodaq_utils.utils import ThreadCommand
from pymodaq.utils.data import DataFromPlugins, DataToExport
from pymodaq.control_modules.viewer_utility_classes import DAQ_Viewer_base, comon_parameters, main

from pymodaq_plugins_lasercontrol.hardware.sim import LaserSimModel


class DAQ_0DViewer_LaserSim(DAQ_Viewer_base):
    params = comon_parameters + [
        {'title': 'Frame period (ms):', 'name': 'wait_time', 'type': 'int', 'value': 500, 'min': 10},
        {'title': 'Noise scale:', 'name': 'noise', 'type': 'float', 'value': 1.0, 'min': 0.0},
        {'title': 'Drift amplitude (µm):', 'name': 'drift_amplitude', 'type': 'float', 'value': 6.0, 'min': 0.0},
        {'title': 'Drift period (s):', 'name': 'drift_period_s', 'type': 'float', 'value': 120.0, 'min': 1.0},
        {'title': 'Intensity drops / min:', 'name': 'dropout_rate_per_min', 'type': 'float', 'value': 0.0,
         'min': 0.0},
        {'title': 'Mute (B silent):', 'name': 'muted', 'type': 'bool', 'value': False},
        {'title': 'Freeze (frozen frames):', 'name': 'frozen', 'type': 'bool', 'value': False},
    ]

    MODEL_PARAMS = ('noise', 'drift_amplitude', 'drift_period_s', 'dropout_rate_per_min', 'muted', 'frozen')

    def ini_attributes(self):
        self.controller: LaserSimModel = None
        self._pending_retry = False

    def _push_model_settings(self):
        for name in self.MODEL_PARAMS:
            setattr(self.controller, name, self.settings[name])

    def commit_settings(self, param):
        if param.name() in self.MODEL_PARAMS and self.controller is not None:
            setattr(self.controller, param.name(), param.value())
        if param.name() == 'wait_time':
            self.emit_status(ThreadCommand('update_main_settings', [['wait_time'], param.value(), 'value']))

    def ini_detector(self, controller=None):
        if self.is_master:
            self.controller = LaserSimModel.instance()
        else:
            self.controller = controller
        self._push_model_settings()
        self.emit_status(ThreadCommand('update_main_settings', [['wait_time'], self.settings['wait_time'], 'value']))
        return 'LaserSim 0D detector ready', True

    def close(self):
        pass

    @staticmethod
    def _to_dte(frame) -> DataToExport:
        data = [DataFromPlugins(name=name, data=[np.array([float(v)]) for v in values.values()],
                                dim='Data0D', labels=list(values.keys()))
                for name, values in frame.items()]
        return DataToExport('LaserSim', data=data)

    def grab_data(self, Naverage=1, **kwargs):
        frame = self.controller.frame()
        if frame is None:            # muted: no data until unmuted (like a silent computer B)
            if not self._pending_retry:
                self._pending_retry = True
                QtCore.QTimer.singleShot(200, self._retry)
            return
        self.dte_signal.emit(self._to_dte(frame))

    def _retry(self):
        self._pending_retry = False
        self.grab_data()

    def stop(self):
        return ''


if __name__ == '__main__':
    main(__file__)
