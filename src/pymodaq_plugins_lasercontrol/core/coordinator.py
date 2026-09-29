"""The core: glue between broker, ownership, safety, state machine and automations.

Policy implemented here (specification D4, D5, O3-A, O10-A, S1-S12):

* **D4 / O3-A** - the last activated automation takes the shared motors; the previous owner goes
  PAUSED and resumes by itself (integral reset) when the motors are released.
* **D5 / S7** - a human action on a motor stops (not pauses) every automation using that motor,
  *before* the manual order is sent. A move ordered outside the extension (DAQ_Move UI) is
  detected afterwards and stops them too.
* **S1 / S2** - before each tick: fresh data and intensity above the threshold, otherwise HELD.
* **O10-A** - HELD automations only restart with :meth:`resume` (manual, after checks).
* **S8** - an exception in an engine puts it in ERROR, once, without retry.
* **S9** - nothing starts by itself: automations only start through :meth:`start`.
* **S11** - :meth:`emergency_stop` stops every automation, then every motor, then latches ESTOP.
"""
from __future__ import annotations

import math
import traceback
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .automation import Automation
from .broker import HardwareBroker
from .clock import Clock
from .events import EventBus, EventKind, Level
from .log import DedupLogger, module_logger
from .ownership import HUMAN, Ownership
from .roles import RoleMap
from .safety import SafetyConfig, SafetyPolicy, SafetySupervisor, Violation
from .signals import SignalSource, is_finite
from .states import AutoState, ENGAGED_STATES, GlobalState, aggregate

logger = DedupLogger(module_logger(__file__))

ML_STATES = ('unknown', 'ML_ON', 'ML_OFF', 'UNSTABLE')
ML_LABELS = {'unknown': 'ML ?', 'ML_ON': 'ML ON', 'ML_OFF': 'ML OFF', 'UNSTABLE': 'Unstable'}
ML_CODES = {'unknown': math.nan, 'ML_ON': 1.0, 'ML_OFF': 0.0, 'UNSTABLE': 0.5}

ConfirmHandler = Callable[[Automation, str, Callable[[bool], None]], None]


class LaserCore:
    def __init__(self, clock: Optional[Clock] = None, roles: Optional[RoleMap] = None,
                 safety_config: Optional[SafetyConfig] = None, events: Optional[EventBus] = None):
        self.clock = clock or Clock()
        self.events = events or EventBus(self.clock)
        self.roles = roles or RoleMap()
        self.signals = SignalSource(self.clock)
        self.ownership = Ownership()
        self.safety = SafetySupervisor(self.clock, self.events, self.signals, self.roles, safety_config)
        self.broker = HardwareBroker(self.clock, self.events, self.ownership, self.safety, self.roles,
                                     self.signals)
        self.broker.core = self
        self.signals.set_heartbeats(self.roles.heartbeats)
        self.automations: Dict[str, Automation] = {}
        self.estop = False
        self.global_state = GlobalState.IDLE
        self.ml_state = 'unknown'
        self.confirm_handler: Optional[ConfirmHandler] = None
        self.shake_handler: Optional[Callable[[], None]] = None
        self.preset_name = ''
        self.preset_hash = ''

    # ================================================================== configuration
    def set_roles(self, roles: RoleMap):
        self.roles = roles
        self.safety.roles = roles
        self.safety.guard.roles = roles
        self.broker.roles = roles
        self.signals.set_heartbeats(roles.heartbeats)

    def set_safety_config(self, cfg: SafetyConfig):
        self.safety.set_config(cfg, self.roles)

    # ================================================================== registry
    def add_automation(self, a: Automation) -> Automation:
        old = self.automations.get(a.id)
        if old is not None and old is not a and old.state != AutoState.OFF:
            raise RuntimeError(f"{a.id} is engaged: stop it before replacing it")
        self.automations[a.id] = a
        return a

    def remove_automation(self, auto_id: str):
        a = self.automations.get(auto_id)
        if a is None:
            return
        if a.state != AutoState.OFF:
            self.stop(auto_id, 'removed')
        self.automations.pop(auto_id, None)

    def get(self, auto_id: str) -> Optional[Automation]:
        return self.automations.get(auto_id)

    def engaged(self) -> List[Automation]:
        return [a for a in self.automations.values() if a.state in ENGAGED_STATES]

    # ================================================================== helpers
    def modules_of(self, a: Automation, wanted_only: bool = False) -> List[str]:
        roles = a.wanted_motor_roles() if wanted_only else a.motor_roles()
        mods = []
        for r in roles:
            m = self.roles.module_for(r)
            if m is not None and m not in mods:
                mods.append(m)
        return mods

    def role_label(self, module: str) -> str:
        return self.roles.label_for_module(module)

    def owner_label(self, module: str) -> str:
        owner = self.ownership.owner_of(module)
        st = self.broker.state(module)
        if st is not None and st.busy and st.mover and str(st.mover).startswith(HUMAN):
            return 'Human'
        if owner is None:
            return 'Free'
        a = self.automations.get(owner)
        return a.label if a else owner

    def _set_state(self, a: Automation, state: AutoState, reason: str = '', level: Level = Level.INFO,
                   kind: str = EventKind.AUTOMATION_STATE, publish: bool = True):
        old = a.state
        a.state = state
        a.reason = reason
        if state != AutoState.PAUSED:
            a.pause_kind = None
            a.paused_by = None
        if publish and (old != state or reason):
            text = f"{a.label}: {old.value} → {state.value}" + (f" ({reason})" if reason else '')
            self.events.publish(kind, a.id, text, level, old=old.value, new=state.value, reason=reason)
        self._update_global_state()

    def _update_global_state(self):
        g = aggregate(self.estop, [a.state for a in self.automations.values()])
        if g != self.global_state:
            old = self.global_state
            self.global_state = g
            lvl = Level.ALARM if g in (GlobalState.ESTOP, GlobalState.SAFETY_HOLD, GlobalState.ERROR) else Level.INFO
            self.events.publish(EventKind.AUTOMATION_STATE, 'CORE', f"Global state: {old.value} → {g.value}",
                                lvl, old=old.value, new=g.value, scope='global')

    # ================================================================== validation
    def validate(self, a: Automation) -> List[str]:
        errors: List[str] = []
        policy = a.policy()
        seen_modules = {}
        for role in a.motor_roles():
            module = self.roles.module_for(role)
            if module is None:
                errors.append(f"motor role '{role}' not declared on this machine")
                continue
            if module in seen_modules:
                errors.append(f"'{role}' and '{seen_modules[module]}' drive the same module {module}")
            seen_modules[module] = role
            if not self.broker.has_module(module):
                errors.append(f"{role}: module '{module}' missing from the dashboard")
                continue
            if not self.broker.is_initialized(module):
                errors.append(f"{role}: module '{module}' not initialized")
                continue
            if self.broker.position(module) is None:
                errors.append(f"{role}: unknown position (read the position)")
        for role in a.signal_roles():
            ch = self.roles.channel_for(role)
            if ch is None:
                errors.append(f"signal role '{role}' not declared on this machine")
            elif not self.signals.known(ch):
                errors.append(f"{role}: channel '{ch}' never received (is acquisition running?)")
            elif policy.check_data:
                ok, why = self.signals.freshness(ch)
                if not ok:
                    errors.append(f"{role}: {why}")
        errors += self.safety.config_errors(a.motor_roles(), policy)
        if policy.check_intensity and self.safety.config.intensity.enabled and self.safety.config.intensity.role:
            ch = self.safety.guard.channel()
            if ch is None:
                pass  # already reported by config_errors
            elif not self.signals.is_fresh(ch):
                errors.append(f"intensity guard: {self.signals.freshness(ch)[1]}")
            elif self.will_capture_intensity_reference(a):
                v = self.safety.guard.value()
                if not (is_finite(v) and v > 0):
                    errors.append("intensity guard: zero or invalid intensity, no reference possible "
                                  "(laser off?)")
            elif self.safety.guard.evaluate() == 'low':
                errors.append("intensity guard: intensity below threshold")
        try:
            errors += list(a.validate(self) or [])
        except Exception as e:  # pragma: no cover
            errors.append(f"internal validation: {e}")
        return errors

    # ================================================================== lifecycle
    def start(self, auto_id: str) -> Tuple[bool, str]:
        a = self.automations.get(auto_id)
        if a is None:
            return False, f"unknown automation: {auto_id}"
        if self.estop:
            return False, "EMERGENCY STOP active: click 'Re-arm' first"
        if a.state in ENGAGED_STATES:
            return False, f"{a.label} is already engaged ({a.state.value})"
        errors = self.validate(a)
        if errors:
            msg = ' ; '.join(errors)
            self.events.publish(EventKind.AUTOMATION_START, a.id, f"Start refused: {msg}", Level.WARNING,
                                errors=errors)
            return False, msg
        policy = a.policy()
        self._set_state(a, AutoState.ARMED, publish=False)
        positions = {m: self.broker.position(m) for m in self.modules_of(a)}
        explicit = a.explicit_ranges(self) if policy.envelope == 'explicit' else None
        self.safety.engage(a.id, positions, explicit=explicit, reset=True)
        if self.will_capture_intensity_reference(a):
            if not self.safety.guard.capture_reference():
                self._abort_start(a)
                return False, "intensity guard: cannot capture the reference"
            self.events.publish(EventKind.SAFETY_INFO, 'SAFETY',
                                f"Intensity reference captured: {self.safety.guard.reference:.4g}")
        try:
            err = a.on_start(self)
        except Exception as e:
            err = f"error at start: {e}"
            logger.exception(f'{a.id} on_start failed')
        if err:
            self._abort_start(a)
            self.events.publish(EventKind.AUTOMATION_START, a.id, f"Start refused: {err}", Level.WARNING)
            return False, err
        a.started_at = self.clock.now()
        a.last_error = ''
        a.next_due = self.clock.now()
        if a.acquire_at_start:
            self.acquire(a)
        self._set_state(a, AutoState.ACTIVE, kind=EventKind.AUTOMATION_START, reason='started')
        self._try_resume_paused()
        return True, ''

    def will_capture_intensity_reference(self, a: Automation) -> bool:
        """S2, relative mode without fixed reference: the reference is the intensity at engagement,
        captured when the first automation checking the intensity starts; it is kept while any such
        automation stays engaged (held ones included, so that a resume compares to it)."""
        cfg = self.safety.config.intensity
        if not (a.policy().check_intensity and cfg.enabled and cfg.mode == 'relative' and cfg.reference is None):
            return False
        if self.safety.guard.captured_reference is None:
            return True
        return not any(o is not a and o.state in ENGAGED_STATES and o.policy().check_intensity
                       for o in self.automations.values())

    def _abort_start(self, a: Automation):
        self.safety.disengage(a.id)
        self.ownership.release(a.id)
        self._set_state(a, AutoState.OFF, publish=False)

    def stop(self, auto_id: str, reason: str = 'stop requested', level: Level = Level.INFO,
             kind: str = EventKind.AUTOMATION_STOP) -> bool:
        a = self.automations.get(auto_id)
        if a is None or a.state == AutoState.OFF:
            return False
        try:
            a.on_stop(self)   # must not move anything (D6/S10)
        except Exception as e:  # pragma: no cover
            logger.error(f'{a.id} on_stop failed: {e}')
        self.ownership.release(a.id)
        self.safety.disengage(a.id)
        self._set_state(a, AutoState.OFF, reason, level, kind)
        self._try_resume_paused()
        return True

    def stop_all_automations(self, reason: str = 'general stop'):
        for a in list(self.automations.values()):
            if a.state != AutoState.OFF:
                self.stop(a.id, reason)

    def hold(self, a: Automation, violation: Violation):
        if a.state not in (AutoState.ACTIVE, AutoState.ARMED, AutoState.PAUSED):
            return
        try:
            a.on_hold(self, violation)
        except Exception as e:  # pragma: no cover
            logger.error(f'{a.id} on_hold failed: {e}')
        self._set_state(a, AutoState.HELD, violation.message, Level.ALARM, EventKind.SAFETY_HOLD)

    def resume(self, auto_id: str) -> Tuple[bool, str]:
        """Manual resume after a safety hold (O10-A) or after an error-free pause."""
        a = self.automations.get(auto_id)
        if a is None:
            return False, 'unknown'
        if self.estop:
            return False, "EMERGENCY STOP active: re-arm first"
        if a.state not in (AutoState.HELD, AutoState.PAUSED):
            return False, f"{a.label} is neither held nor paused ({a.state.value})"
        if a.state == AutoState.PAUSED and a.pause_kind == 'confirm':
            return False, "a move confirmation is pending"
        policy = a.policy()
        errors = []
        for role in a.signal_roles():
            ch = self.roles.channel_for(role)
            ok, why = self.signals.freshness(ch)
            if policy.check_data and not ok:
                errors.append(why)
        if policy.check_intensity:
            ok, why = self.safety.guard.can_resume()
            if not ok:
                errors.append(why)
        for m in self.modules_of(a):
            if self.broker.position(m) is None:
                errors.append(f"{self.role_label(m)}: unknown position")
        if errors:
            msg = ' ; '.join(errors)
            self.events.publish(EventKind.SAFETY_RESUME, a.id, f"Resume refused: {msg}", Level.WARNING)
            return False, msg
        self.acquire(a)       # D4: resuming is an activation -> takes the motors back
        try:
            a.on_resume(self)
        except Exception as e:  # pragma: no cover
            self.error(a, e)
            return False, str(e)
        a.next_due = self.clock.now()
        self._set_state(a, AutoState.ACTIVE, 'manual resume', Level.INFO, EventKind.SAFETY_RESUME)
        return True, ''

    def reset_reference(self, auto_id: str) -> bool:
        """New S4 envelope reference = current positions (explicit operator action)."""
        a = self.automations.get(auto_id)
        if a is None or a.state == AutoState.OFF:
            return False
        positions = {m: self.broker.position(m) for m in self.modules_of(a)}
        self.safety.engage(a.id, positions, reset=True)
        self.events.publish(EventKind.SAFETY_INFO, a.id, f"{a.label}: new envelope reference")
        return True

    def error(self, a: Automation, exc: BaseException):
        """S8: freeze the automation in ERROR (no retry, logged once)."""
        text = f"{type(exc).__name__}: {exc}"
        a.last_error = text
        logger.error(f'{a.id} error: {text}\n{traceback.format_exc()}')
        self._set_state(a, AutoState.ERROR, text, Level.ERROR, EventKind.ERROR)

    # ================================================================== ownership policy
    def acquire(self, a: Automation, roles: Optional[Iterable[str]] = None) -> Dict[str, Optional[str]]:
        """Take the motors (D4: last activated wins). Previous owners are paused (O3-A)."""
        if roles is None:
            modules = self.modules_of(a, wanted_only=True)
        else:
            modules = [m for m in (self.roles.module_for(r) for r in roles) if m]
        changed = self.ownership.acquire(a.id, modules)
        by_prev: Dict[str, List[str]] = {}
        for module, prev in changed.items():
            if prev is not None and prev != a.id:
                by_prev.setdefault(prev, []).append(module)
        for prev_id, mods in by_prev.items():
            prev = self.automations.get(prev_id)
            if prev is not None:
                self._preempted(prev, a, mods)
        return changed

    def release(self, a: Automation, roles: Optional[Iterable[str]] = None):
        if roles is None:
            self.ownership.release(a.id)
        else:
            self.ownership.release(a.id, [m for m in (self.roles.module_for(r) for r in roles) if m])
        self._try_resume_paused()

    def _preempted(self, prev: Automation, by: Automation, modules: List[str]):
        names = ', '.join(self.role_label(m) for m in modules)
        if prev.state in (AutoState.ACTIVE, AutoState.ARMED) or \
                (prev.state == AutoState.PAUSED and prev.pause_kind == 'confirm'):
            prev.state = AutoState.PAUSED
            prev.pause_kind = 'preempted'
            prev.paused_by = by.id
            prev.reason = f"preempted by {by.label} ({names})"
            try:
                prev.on_pause(self)
            except Exception as e:  # pragma: no cover
                logger.error(f'{prev.id} on_pause failed: {e}')
            self.events.publish(EventKind.PREEMPTION, prev.id,
                                f"{prev.label} paused: {by.label} took {names}", Level.WARNING,
                                by=by.id, modules=modules)
            self._update_global_state()
        else:
            self.events.publish(EventKind.PREEMPTION, prev.id,
                                f"{prev.label} ({prev.state.value}) loses {names} to {by.label}",
                                Level.WARNING, by=by.id, modules=modules)

    def _try_resume_paused(self):
        if self.estop:
            return
        for a in list(self.automations.values()):
            if a.state != AutoState.PAUSED or a.pause_kind != 'preempted':
                continue
            wanted = self.modules_of(a, wanted_only=True)
            if any(self.ownership.owner_of(m) not in (None, a.id) for m in wanted):
                continue
            self.ownership.acquire(a.id, wanted)
            try:
                a.on_resume(self)
            except Exception as e:  # pragma: no cover
                self.error(a, e)
                continue
            a.next_due = self.clock.now()
            self._set_state(a, AutoState.ACTIVE, 'automatic resume (motors released)', Level.INFO,
                            EventKind.PREEMPTION)

    # ================================================================== human / hardware notifications
    def human_preempt(self, module: str, source: str):
        """D5/S7: called by the broker BEFORE a manual order is sent."""
        victims = [a for a in self.automations.values()
                   if a.state != AutoState.OFF and module in self.modules_of(a)]
        role = self.role_label(module)
        for a in victims:
            self.stop(a.id, f"manual action on {role} ({source})", Level.WARNING, EventKind.PREEMPTION)

    def on_external_move(self, module: str, position: Optional[float]):
        role = self.role_label(module)
        victims = [a for a in self.automations.values()
                   if a.state != AutoState.OFF and module in self.modules_of(a)]
        pos = f"{position:.6g}" if position is not None and is_finite(position) else '?'
        self.events.publish(EventKind.MOVE_EXTERNAL, 'BROKER',
                            f"External move detected on {role} (position {pos})",
                            Level.WARNING if victims else Level.INFO, module=module)
        for a in victims:
            self.stop(a.id, f"external move detected on {role} (outside the extension)", Level.WARNING,
                      EventKind.PREEMPTION)

    def on_move_timeout(self, module: str, mover: Optional[str]):
        role = self.role_label(module)
        timeout = self.safety.config.move_timeout_s
        self.events.publish(EventKind.MOVE_TIMEOUT, 'BROKER',
                            f"{role}: move not finished after {timeout:.0f} s", Level.ALARM,
                            module=module)
        a = self.automations.get(mover) if mover else None
        if a is not None:
            self.hold(a, Violation('timeout', f"timeout on {role} (> {timeout:.0f} s)", role))

    # ================================================================== moves requested by engines
    def request_moves(self, a: Automation, moves: Dict[str, float]) -> Dict[str, float]:
        """Checked relative moves for an automation. Returns the deltas actually sent.
        Never stacks: if one of the motors is busy nothing is sent (the engine retries next tick)."""
        if self.estop or a.state != AutoState.ACTIVE:
            return {}
        policy = a.policy()
        if policy.check_data:
            ok, reasons = self.signals.all_fresh(
                [self.roles.channel_for(r) for r in a.signal_roles() if self.roles.channel_for(r)])
            if not ok:
                self.hold(a, Violation('stale', 'stale data: ' + ' ; '.join(reasons)))
                return {}
        if policy.check_intensity and self.safety.config.intensity.enabled:
            guard_state = self.safety.guard.evaluate()
            if guard_state in ('low', 'unknown'):
                self.hold(a, Violation('intensity', 'intensity below threshold' if guard_state == 'low'
                                       else 'unknown intensity'))
                return {}
        plan: Dict[str, Tuple[str, float]] = {}
        envelope_violation = None
        for role, delta in moves.items():
            try:
                delta = float(delta)
            except (TypeError, ValueError):
                delta = math.nan
            if delta == 0.0:
                continue
            module = self.roles.module_for(role)
            if module is None or not self.broker.has_module(module):
                self.hold(a, Violation('config', f"role {role} not resolved", role))
                return {}
            if self.ownership.owner_of(module) != a.id:          # S6
                self.hold(a, Violation('owner', f"{a.label} does not own {role}", role))
                return {}
            if self.broker.is_busy(module):                      # T4.1: skip, never stack
                return {}
            allowed, violation = self.safety.check_move(a.id, role, module, self.broker.position(module),
                                                        delta, policy)
            if violation is not None and violation.code != 'envelope':
                self.hold(a, violation)
                return {}
            if violation is not None:
                envelope_violation = violation
            lim = self.safety.limits(role)
            tiny = max(lim.resolution if lim else 0.0, 1e-12)
            if abs(allowed) >= tiny:
                plan[role] = (module, allowed)
        sent: Dict[str, float] = {}
        for role, (module, d) in plan.items():
            if self.broker.send_relative(a.id, module, d):
                self.safety.record_move(module, d)
                sent[role] = d
        if envelope_violation is not None:
            self.hold(a, envelope_violation)
        return sent

    def any_busy(self, roles: Iterable[str]) -> bool:
        return any(self.broker.is_busy(self.roles.module_for(r)) for r in roles)

    def position(self, role: str) -> Optional[float]:
        return self.broker.position(self.roles.module_for(role))

    def last_done_t(self, role: str) -> Optional[float]:
        st = self.broker.state(self.roles.module_for(role) or '')
        return st.last_done_t if st else None

    def request_confirmation(self, a: Automation, text: str, moves: Dict[str, float]):
        """'Confirm moves' mode: the automation is explicitly PAUSED while the dialog is open."""
        if self.confirm_handler is None:
            self.stop(a.id, "confirmation impossible (no user interface)", Level.WARNING)
            return
        a.state = AutoState.PAUSED
        a.pause_kind = 'confirm'
        a.reason = 'waiting for move confirmation'
        self._update_global_state()

        def answer(accepted: bool, a=a, moves=dict(moves)):
            if a.state != AutoState.PAUSED or a.pause_kind != 'confirm':
                return  # stale answer (stopped / preempted meanwhile)
            if not accepted:
                self.stop(a.id, "move refused by the user", Level.INFO)
                return
            self._set_state(a, AutoState.ACTIVE, publish=False)
            self.request_moves(a, moves)

        try:
            self.confirm_handler(a, text, answer)
        except Exception as e:  # pragma: no cover
            self.error(a, e)

    # ================================================================== emergency stop
    def emergency_stop(self, source: str = 'UI'):
        self.estop = True                                     # latch first: nothing can be sent anymore
        for a in list(self.automations.values()):             # 1. all automations
            if a.state != AutoState.OFF:
                self.stop(a.id, "EMERGENCY STOP", Level.ALARM, EventKind.ESTOP)
        self.broker.stop_all("EMERGENCY STOP")               # 2. every motor of the dashboard
        self.events.publish(EventKind.ESTOP, source, "EMERGENCY STOP: automations and motors stopped",
                            Level.ALARM)
        self._update_global_state()                           # 3. ESTOP state (manual exit only)

    def reset_estop(self, source: str = 'UI'):
        if not self.estop:
            return
        self.estop = False
        self.events.publish(EventKind.ESTOP_RESET, source, "Emergency stop re-armed", Level.INFO)
        self._update_global_state()

    # ================================================================== annotations
    def set_ml_state(self, state: str, source: str = 'UI'):
        if state not in ML_STATES:
            raise ValueError(state)
        old = self.ml_state
        self.ml_state = state
        self.events.publish(EventKind.ML, source, f"Mode-lock state: {ML_LABELS[old]} → {ML_LABELS[state]}",
                            Level.INFO, old=old, new=state)

    def add_note(self, text: str, source: str = 'UI'):
        text = (text or '').strip()
        if text:
            self.events.publish(EventKind.NOTE, source, text, Level.INFO)

    def shake(self, source: str = 'UI') -> bool:
        """Piezo shake (T7.3): the event is published first (the recorder takes its 'before'
        snapshot synchronously), then the shaker is fired."""
        if self.estop:
            return False
        self.events.publish(EventKind.SHAKE, source, 'Piezo shake', Level.INFO)
        if self.shake_handler is not None:
            try:
                self.shake_handler()
            except Exception as e:
                self.events.publish(EventKind.ERROR, 'SHAKER', f'Shake impossible: {e}', Level.ERROR)
                return False
        return True

    # ================================================================== scheduler
    def tick(self, now: Optional[float] = None):
        now = self.clock.now() if now is None else now
        self.broker.tick(now, self.safety.config.move_timeout_s)
        guard_state = self.safety.intensity_state()
        for a in list(self.automations.values()):
            if a.state != AutoState.ACTIVE or now < a.next_due:
                continue
            a.next_due = now + max(a.period_s, 0.0)
            policy = a.policy()
            if policy.check_data:
                channels = [self.roles.channel_for(r) for r in a.signal_roles()]
                if any(c is None for c in channels):
                    self.hold(a, Violation('config', 'signal role not declared'))
                    continue
                ok, reasons = self.signals.all_fresh(channels)
                if not ok:
                    self.hold(a, Violation('stale', 'stale data: ' + ' ; '.join(reasons)))
                    continue
            if policy.check_intensity and self.safety.config.intensity.enabled:
                if guard_state == 'low':
                    self.hold(a, Violation('intensity', f"intensity below threshold "
                                                        f"({self.safety.guard.value():.4g})"))
                    continue
                if guard_state == 'unknown':
                    self.hold(a, Violation('intensity', "unknown intensity (signal missing or stale)"))
                    continue
            try:
                a.tick(self, now)
            except Exception as e:
                self.error(a, e)
                continue
            if a.state == AutoState.ACTIVE and a.is_done():
                self.stop(a.id, 'finished')
        self._update_global_state()
