"""Hardware side: PyMoDAQ ports (the only code touching DAQ_Move/DAQ_Viewer objects), the laser
simulator used by the LaserSim mock plugins, and the Red Pitaya piezo shaker.

Submodules are imported explicitly (``from .hardware.pymodaq_ports import ...``) so that the pure
parts (``channels``, ``sim``) stay importable without Qt."""
