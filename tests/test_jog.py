"""Jog window: the axes of the old Alignment tab give the first groups of a preset without a jog section."""
import pytest

pytest.importorskip('pymodaq_plugins_keyboardjog')

from pymodaq_plugins_keyboardjog.core.model import HORIZONTAL, VERTICAL  # noqa: E402
from pymodaq_plugins_lasercontrol.ui.jog import setup_from_alignment  # noqa: E402


def test_groups_from_old_alignment_axes():
    alignment = {'axes': [
        {'name': 'OC', 'h': {'actuator': 'OC_H', 'slow': 100, 'invert': True}, 'v': {'actuator': 'OC_V', 'slow': 100}},
        {'name': 'Detuning', 'h': {'actuator': 'Detuning', 'slow': 0.033}, 'v': None},
        {'name': 'Unassigned', 'h': {'actuator': 'None'}, 'v': None},
        {'name': 'Gas Jet z', 'h': None, 'v': {'actuator': 'Jet_Z', 'slow': 0.1, 'invert': True}},
        {'name': 'Twice', 'h': {'actuator': 'OC_H', 'slow': 5}, 'v': None},      # already used: ignored
    ]}
    setup, inverted = setup_from_alignment(alignment)
    assert setup.pair_numbers == {'OC_H': 1, 'OC_V': 1, 'Detuning': 2, 'Jet_Z': 4}   # number = old axis key
    assert setup.axes == {'OC_H': HORIZONTAL, 'OC_V': VERTICAL, 'Detuning': HORIZONTAL, 'Jet_Z': VERTICAL}
    assert setup.steps == {'OC_H': 100.0, 'OC_V': 100.0, 'Detuning': 0.033, 'Jet_Z': 0.1}
    assert setup.pair_names == {1: 'OC'}                     # a name for the pairs only
    assert sorted(inverted) == ['Jet_Z', 'OC_H']
    empty, none = setup_from_alignment({})
    assert not empty.pair_numbers and none == []
