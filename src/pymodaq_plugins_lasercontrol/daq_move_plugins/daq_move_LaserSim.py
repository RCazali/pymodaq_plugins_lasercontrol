"""Simulated motor of the LaserSim laser model (office tests, specification T1.3).

Choose the axis (OC_H, OC_V, EM_H, EM_V, AP_X, AP_Y, JET_X, JET_Y, JET_Z, KERR, and the test
benches STEER_H, STEER_V, PD_X, PD_Y) in the settings;
the ``LaserSim`` 0D detector computes centroids / power / XUV flux from these positions.
Implements ``jog(direction, velocity)`` so that the Alignment tab 'native' jog mode can be tested.
"""
from pymodaq.control_modules.move_utility_classes import (DAQ_Move_base, comon_parameters_fun, main,
                                                          DataActuatorType)
from pymodaq.utils.data import DataActuator

from pymodaq_plugins_lasercontrol.hardware.sim import AXES, UNITS, LaserSimModel


class DAQ_Move_LaserSim(DAQ_Move_base):
    """Simulated motor of the LaserSim laser model: one axis of the shared simulation, no hardware nor driver.

    Tested with the ``LaserSim`` 0D detector of this package (office tests of Laser Control).
    """
    _controller_units = UNITS
    is_multiaxes = True
    _axis_names = list(AXES)
    _epsilons = 0.01
    data_actuator_type = DataActuatorType.DataActuator

    params = [
        {'title': 'Speed (µm/s):', 'name': 'speed', 'type': 'float', 'value': 50.0, 'min': 0.001},
    ] + comon_parameters_fun(is_multiaxes, axis_names=_axis_names, epsilon=_epsilons)

    def ini_attributes(self):
        self.controller: LaserSimModel = None

    def get_actuator_value(self):
        pos = DataActuator(data=self.controller.position(self.axis_name), units=self.axis_unit)
        return self.get_position_with_scaling(pos)

    def close(self):
        pass

    def commit_settings(self, param):
        if param.name() == 'speed' and self.controller is not None:
            self.controller.set_speed(self.axis_name, param.value())

    def ini_stage(self, controller=None):
        if self.is_master:
            self.controller = LaserSimModel.instance()
        else:
            self.controller = controller
        self.controller.set_speed(self.axis_name, self.settings['speed'])
        return f"LaserSim axis {self.axis_name} ready", True

    def move_abs(self, position: DataActuator):
        position = self.check_bound(position)
        self.target_value = position
        position = self.set_position_with_scaling(position)
        self.controller.move_to(self.axis_name, position.value())

    def move_rel(self, position: DataActuator):
        # the simulated axis is shared by every module instance: start from the real position
        self.current_value = self.get_actuator_value()
        position = self.check_bound(self.current_value + position) - self.current_value
        self.target_value = position + self.current_value
        self.set_position_relative_with_scaling(position)
        self.controller.move_to(self.axis_name, self.target_value.value())

    def move_home(self):
        self.controller.move_to(self.axis_name, 0.0)

    def stop_motion(self):
        self.controller.stop(self.axis_name)
        self.move_done()

    def jog(self, direction, velocity):
        """Continuous move until stop_motion (native jog of the Alignment tab)."""
        self.controller.jog(self.axis_name, int(direction), float(velocity))


if __name__ == '__main__':
    main(__file__)
