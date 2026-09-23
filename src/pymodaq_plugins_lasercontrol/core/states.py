"""State machine vocabulary (cahier des charges T3.5)."""
from __future__ import annotations

from enum import Enum
from typing import Iterable


class AutoState(str, Enum):
    """State of one automation (PID_x, optimiser, scan...)."""
    OFF = 'OFF'          # not running, owns nothing
    ARMED = 'ARMED'      # start requested, waiting for its first valid sample (reference capture)
    ACTIVE = 'ACTIVE'    # running, allowed to move its motors
    PAUSED = 'PAUSED'    # preempted by another automation (O3-A) or waiting for a user confirmation
    HELD = 'HELD'        # frozen by a safety rule: manual resume only (O10-A)
    ERROR = 'ERROR'      # exception inside the engine: frozen until stopped (S8)

    def __str__(self):
        return self.value


class GlobalState(str, Enum):
    IDLE = 'IDLE'
    RUNNING = 'RUNNING'
    SAFETY_HOLD = 'SAFETY_HOLD'
    ESTOP = 'ESTOP'
    ERROR = 'ERROR'

    def __str__(self):
        return self.value


RUNNING_STATES = (AutoState.ARMED, AutoState.ACTIVE, AutoState.PAUSED)
ENGAGED_STATES = (AutoState.ARMED, AutoState.ACTIVE, AutoState.PAUSED, AutoState.HELD, AutoState.ERROR)

STATE_COLORS = {
    AutoState.OFF: '#7f8c8d',
    AutoState.ARMED: '#2980b9',
    AutoState.ACTIVE: '#27ae60',
    AutoState.PAUSED: '#f39c12',
    AutoState.HELD: '#e74c3c',
    AutoState.ERROR: '#8e44ad',
    GlobalState.IDLE: '#7f8c8d',
    GlobalState.RUNNING: '#27ae60',
    GlobalState.SAFETY_HOLD: '#e74c3c',
    GlobalState.ESTOP: '#c0392b',
    GlobalState.ERROR: '#8e44ad',
}

STATE_LABELS_FR = {
    AutoState.OFF: 'Arrêté',
    AutoState.ARMED: 'Armement',
    AutoState.ACTIVE: 'Actif',
    AutoState.PAUSED: 'En pause',
    AutoState.HELD: 'Gelé (sécurité)',
    AutoState.ERROR: 'Erreur',
    GlobalState.IDLE: 'Repos',
    GlobalState.RUNNING: 'En marche',
    GlobalState.SAFETY_HOLD: 'Gel sécurité',
    GlobalState.ESTOP: 'ARRÊT D’URGENCE',
    GlobalState.ERROR: 'Erreur',
}


def aggregate(estop: bool, states: Iterable[AutoState]) -> GlobalState:
    """Global state from the automations' states (priority ESTOP > ERROR > SAFETY_HOLD > RUNNING)."""
    states = list(states)
    if estop:
        return GlobalState.ESTOP
    if any(s == AutoState.ERROR for s in states):
        return GlobalState.ERROR
    if any(s == AutoState.HELD for s in states):
        return GlobalState.SAFETY_HOLD
    if any(s in RUNNING_STATES for s in states):
        return GlobalState.RUNNING
    return GlobalState.IDLE
