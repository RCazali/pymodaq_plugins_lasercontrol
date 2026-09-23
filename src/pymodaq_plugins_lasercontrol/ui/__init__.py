"""Qt user interface of Laser Control (qtpy: PyQt5 / PyQt6 / PySide6).

Rule: widgets never touch PyMoDAQ modules; they talk to the core (``app.core``) and to the
:class:`ManualControl` facade, which both go through the broker."""
