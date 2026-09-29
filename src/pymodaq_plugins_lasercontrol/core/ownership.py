"""Motor ownership registry (specification T3.3, invariant S6).

One owner per *physical* motor (DAQ_Move module) at any time. Owners are automation ids
(``PID:TipTilt``, ``OPT``, ``SCAN`` ...). A human action is not a persistent owner: it preempts
(stops) the automation owning the motor before the order is sent (D5/S7), see the core.
The policy (who pauses, who stops) lives in :class:`LaserCore`; this class only keeps the table
and notifies listeners.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Set

HUMAN = 'HUMAN'

Listener = Callable[[str, Optional[str], Optional[str]], None]  # (module, old_owner, new_owner)


class Ownership:
    def __init__(self):
        self._owner: Dict[str, str] = {}
        self._listeners: List[Listener] = []

    def add_listener(self, cb: Listener):
        self._listeners.append(cb)

    def _notify(self, module: str, old: Optional[str], new: Optional[str]):
        for cb in list(self._listeners):
            try:
                cb(module, old, new)
            except Exception:  # pragma: no cover - defensive
                pass

    def owner_of(self, module: str) -> Optional[str]:
        return self._owner.get(module)

    def modules_of(self, owner: str) -> Set[str]:
        return {m for m, o in self._owner.items() if o == owner}

    def owns_all(self, owner: str, modules: Iterable[str]) -> bool:
        return all(self._owner.get(m) == owner for m in modules)

    def acquire(self, owner: str, modules: Iterable[str]) -> Dict[str, Optional[str]]:
        """Take the modules (preemption). Returns ``{module: previous owner}`` for the modules
        that changed hands (previous owner may be None)."""
        changed = {}
        for m in modules:
            old = self._owner.get(m)
            if old != owner:
                self._owner[m] = owner
                changed[m] = old
                self._notify(m, old, owner)
        return changed

    def release(self, owner: str, modules: Optional[Iterable[str]] = None) -> List[str]:
        mods = self.modules_of(owner) if modules is None else [m for m in modules
                                                               if self._owner.get(m) == owner]
        for m in mods:
            del self._owner[m]
            self._notify(m, owner, None)
        return list(mods)

    def table(self) -> Dict[str, str]:
        return dict(self._owner)

    def clear(self):
        for m, o in list(self._owner.items()):
            del self._owner[m]
            self._notify(m, o, None)
