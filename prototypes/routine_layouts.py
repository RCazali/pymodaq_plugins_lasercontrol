"""Prototype (not part of the plugin): four ways to SHOW a routine as a diagram, to choose one before it is
built into the Routines tab. Same routines as the "Simulator demo" preset, same model (engines/routine.py).

    python prototypes/routine_layouts.py            (window 1300 x 700, like the lab screen)

A  Vertical flowchart   one box per step, its conditions inside, loops drawn in lanes on the right
B  Horizontal timeline  steps left to right, loops as arcs above, END / FAIL below (fits the bottom of the screen)
C  List + jump arrows   the current list, with the "go to" drawn in a gutter on the left (most compact)
D  Structured blocks    no "go to": loops shown as REPEAT blocks, waits inline (a different editing model)

"▶ Simulate" plays a typical run: the current step is marked ▶ with a thick bright frame (never colour only).
Click a step: its details on the right (where the editor would be).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from qtpy import QtCore, QtGui, QtWidgets  # noqa: E402

from pymodaq_plugins_lasercontrol.engines.routine import (Routine, describe_action, describe_condition,  # noqa: E402
                                                          describe_until)
from pymodaq_plugins_lasercontrol.tools.sim_preset import demo_routines  # noqa: E402

# goggles: dark background, bright foregrounds, states by shape + text, never by hue alone
BG = QtGui.QColor('#1d1f24')
BOX = QtGui.QColor('#2c2f38')
BOX_WAIT = QtGui.QColor('#262a33')
TEXT = QtGui.QColor('#f2f2f2')
DIM = QtGui.QColor('#a9afbb')
EDGE = QtGui.QColor('#c8ccd4')
LOOP = QtGui.QColor('#66d9ff')
CURRENT = QtGui.QColor('#ffd75e')
END_C = QtGui.QColor('#ffffff')
FAIL_C = QtGui.QColor('#ffb347')

#: a typical run of each demo routine (step indexes, then END / FAIL)
RUNS = {'HA_opt': list(range(11)) + ['END'],
        'Keep_aligned': [0, 1, 2, 3] + [4, 5, 6, 7, 8, 9, 10, 11] * 2 + [4],
        'Walk_X': [0, 1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 'END']}


# ====================================================================== model -> graph
def exits(r: Routine, i: int) -> List[Tuple[str, object]]:
    """(label, target) of every way out of step i; target = step index, 'END' or 'FAIL'."""
    n = len(r.steps)
    s = r.steps[i]

    def target(then, goto):
        if then == 'goto':
            return goto - 1
        if then == 'end':
            return 'END'
        if then == 'fail':
            return 'FAIL'
        return i + 1 if i + 1 < n else 'END'
    out = [(describe_condition(c).split(' → ')[0], target(c.then, c.goto)) for c in s.until]
    if s.timeout_s > 0:
        out.append((f'timeout {s.timeout_s:g} s', target(s.on_timeout, s.timeout_goto)))
    if not s.until:
        out.append(('done', target('next', 0)))
    return out


def font(pt=10, bold=False) -> QtGui.QFont:
    f = QtGui.QFont('Segoe UI')
    f.setPointSizeF(pt)
    f.setBold(bold)
    return f


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + '…'


class StepBox(QtWidgets.QGraphicsPathItem):
    """A clickable rounded box; ``set_current`` marks the running step (▶ + thick bright frame)."""

    def __init__(self, rect: QtCore.QRectF, index: int, fill=BOX, radius=8):
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        super().__init__(path)
        self.index = index
        self.setBrush(fill)
        self.setPen(QtGui.QPen(QtGui.QColor('#5a6070'), 1.2))
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.marker = QtWidgets.QGraphicsSimpleTextItem('▶', self)
        self.marker.setFont(font(13, True))
        self.marker.setBrush(CURRENT)
        self.marker.setPos(rect.left() - 20, rect.top() + 2)
        self.marker.setVisible(False)

    def set_current(self, on: bool):
        self.marker.setVisible(on)
        self.setPen(QtGui.QPen(CURRENT, 4) if on else QtGui.QPen(QtGui.QColor('#5a6070'), 1.2))
        self.setZValue(2 if on else 0)


class Diagram(QtWidgets.QGraphicsView):
    step_clicked = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setBackgroundBrush(BG)
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.boxes: Dict[int, StepBox] = {}
        self.terminals: Dict[str, List[QtWidgets.QGraphicsItem]] = {}
        self.scene().selectionChanged.connect(self._on_selection)

    def _on_selection(self):
        for it in self.scene().selectedItems():
            if isinstance(it, StepBox):
                self.step_clicked.emit(it.index)

    # ------------------------------------------------------------------ drawing helpers
    def text(self, x, y, s, pt=10, bold=False, color=TEXT, parent=None):
        t = QtWidgets.QGraphicsSimpleTextItem(s, parent)
        t.setFont(font(pt, bold))
        t.setBrush(color)
        t.setPos(x, y)
        if parent is None:
            self.scene().addItem(t)
        return t

    def arrow(self, pts: List[QtCore.QPointF], color=EDGE, dashed=False, width=1.6, curve=False):
        path = QtGui.QPainterPath(pts[0])
        if curve and len(pts) == 4:
            path.cubicTo(pts[1], pts[2], pts[3])
        else:
            for p in pts[1:]:
                path.lineTo(p)
        pen = QtGui.QPen(color, width)
        if dashed:
            pen.setStyle(QtCore.Qt.DashLine)
        self.scene().addPath(path, pen)
        a, b = (pts[-2], pts[-1])
        line = QtCore.QLineF(b, a)
        line.setLength(9)
        l1, l2 = QtCore.QLineF(line), QtCore.QLineF(line)
        l1.setAngle(line.angle() + 25)
        l2.setAngle(line.angle() - 25)
        head = QtGui.QPolygonF([b, l1.p2(), l2.p2()])
        self.scene().addPolygon(head, QtGui.QPen(color, 1), QtGui.QBrush(color))

    def terminal(self, x, y, kind: str):
        """'✔ END' / '✖ FAIL' tag (shape + text, not colour only)."""
        label = '✔ END' if kind == 'END' else '✖ FAIL'
        c = END_C if kind == 'END' else FAIL_C
        rect = QtCore.QRectF(x, y - 11, 62, 22)
        item = self.scene().addRect(rect, QtGui.QPen(c, 1.5), QtGui.QBrush(QtGui.QColor('#15161a')))
        t = self.text(x + 7, y - 9, label, 9, True, c)
        self.terminals.setdefault(kind, []).extend([item, t])

    def set_current(self, step):
        for i, b in self.boxes.items():
            b.set_current(i == step)
        for kind, items in self.terminals.items():
            for it in items:
                if isinstance(it, QtWidgets.QGraphicsRectItem):
                    it.setPen(QtGui.QPen(CURRENT if kind == step else
                                         (END_C if kind == 'END' else FAIL_C), 4 if kind == step else 1.5))
        if isinstance(step, int) and step in self.boxes:
            self.ensureVisible(self.boxes[step], 40, 40)

    def draw(self, r: Routine):
        self.scene().clear()
        self.boxes, self.terminals = {}, {}
        self.build(r)
        self.setSceneRect(self.scene().itemsBoundingRect().adjusted(-30, -30, 30, 30))

    def build(self, r: Routine):
        raise NotImplementedError


# ====================================================================== A: vertical flowchart
class Flowchart(Diagram):
    W, HEAD, LINE, GAP = 330, 26, 18, 34

    def build(self, r):
        x0, y = 60, 20
        tops, rows = {}, {}
        for i, s in enumerate(r.steps):
            ex = exits(r, i)
            h = self.HEAD + self.LINE * len(ex) + 8
            box = StepBox(QtCore.QRectF(x0, y, self.W, h), i)
            self.scene().addItem(box)
            self.boxes[i] = box
            self.text(x0 + 10, y + 4, f'{i + 1}  {short(describe_action(s), 40)}', 10.5, True, parent=box)
            for k, (label, tgt) in enumerate(ex):
                ty = y + self.HEAD + k * self.LINE
                if tgt in ('END', 'FAIL'):
                    arrow_txt = '✔ end' if tgt == 'END' else '✖ fail'
                else:
                    arrow_txt = '↓ next' if tgt == i + 1 else f'↻ to {tgt + 1}'
                self.text(x0 + 18, ty, f'{"wait: " if k == 0 and label != "done" else "or: " if label != "done" else ""}'
                          f'{short(label, 34)}', 9, False, DIM, parent=box)
                self.text(x0 + self.W - 62, ty, arrow_txt, 9, True,
                          LOOP if isinstance(tgt, int) and tgt != i + 1 else TEXT, parent=box)
                rows[(i, k)] = ty + 8
            tops[i] = (y, h)
            y += h + self.GAP
        lane = 0
        for i in range(len(r.steps)):
            y, h = tops[i]
            drew_next = False
            for k, (label, tgt) in enumerate(exits(r, i)):
                if tgt == i + 1 and not drew_next and i + 1 in tops:
                    self.arrow([QtCore.QPointF(x0 + self.W / 2, y + h), QtCore.QPointF(x0 + self.W / 2, tops[i + 1][0])])
                    drew_next = True
                elif tgt in ('END', 'FAIL'):
                    yy = rows[(i, k)]
                    self.arrow([QtCore.QPointF(x0 + self.W, yy), QtCore.QPointF(x0 + self.W + 40, yy)],
                               END_C if tgt == 'END' else FAIL_C)
                    self.terminal(x0 + self.W + 42, yy, tgt)
                elif isinstance(tgt, int) and tgt != i + 1:
                    lane += 1
                    lx = x0 + self.W + 120 + 26 * lane
                    yy = rows[(i, k)]
                    ty = tops[tgt][0] + 13
                    self.arrow([QtCore.QPointF(x0 + self.W, yy), QtCore.QPointF(lx, yy), QtCore.QPointF(lx, ty),
                                QtCore.QPointF(x0 + self.W + 2, ty)], LOOP, dashed=tgt < i)
                    self.text(lx + 4, (yy + ty) / 2 - 8, f'↻ {tgt + 1}', 8.5, True, LOOP)


# ====================================================================== B: horizontal timeline
class Timeline(Diagram):
    W, H, GAP = 132, 92, 34

    def build(self, r):
        y0 = 230
        xs = {}
        for i, s in enumerate(r.steps):
            x = 30 + i * (self.W + self.GAP)
            xs[i] = x
            box = StepBox(QtCore.QRectF(x, y0, self.W, self.H), i)
            self.scene().addItem(box)
            self.boxes[i] = box
            self.text(x + 8, y0 + 4, f'{i + 1}', 13, True, parent=box)
            words = describe_action(s).split(' ', 1)
            self.text(x + 30, y0 + 6, short(words[0], 12), 10, True, parent=box)
            if len(words) > 1:
                self.text(x + 8, y0 + 28, short(words[1], 19), 9, False, TEXT, parent=box)
            waits = [lab for lab, _ in exits(r, i) if lab != 'done']
            for k, lab in enumerate(waits[:2]):
                self.text(x + 8, y0 + 48 + 18 * k, '⏳ ' + short(lab, 17), 8.5, False, DIM, parent=box)
        span_level = {}
        for i in range(len(r.steps)):
            drew_next = False
            for k, (label, tgt) in enumerate(exits(r, i)):
                x = xs[i]
                if tgt == i + 1 and not drew_next and i + 1 in xs:
                    self.arrow([QtCore.QPointF(x + self.W, y0 + self.H / 2), QtCore.QPointF(xs[i + 1], y0 + self.H / 2)])
                    drew_next = True
                elif tgt in ('END', 'FAIL'):
                    cx = x + (self.W * 0.3 if tgt == 'END' else self.W * 0.75)
                    self.arrow([QtCore.QPointF(cx, y0 + self.H), QtCore.QPointF(cx, y0 + self.H + 60)],
                               END_C if tgt == 'END' else FAIL_C)
                    self.terminal(cx - 31, y0 + self.H + 71, tgt)
                    self.text(cx - 31, y0 + self.H + 86 + (0 if tgt == 'END' else 16), short(label, 22), 8, False, DIM)
                elif isinstance(tgt, int) and tgt != i + 1:
                    lvl = span_level.get((min(i, tgt), max(i, tgt)), 0)
                    span_level[(min(i, tgt), max(i, tgt))] = lvl + 1
                    height = 50 + 22 * abs(i - tgt) + 28 * lvl
                    a = QtCore.QPointF(x + self.W / 2 + 10 * lvl, y0)
                    b = QtCore.QPointF(xs[tgt] + self.W / 2 + 10 * lvl, y0)
                    self.arrow([a, QtCore.QPointF(a.x(), y0 - height), QtCore.QPointF(b.x(), y0 - height), b], LOOP,
                               dashed=True, curve=True)
                    self.text((a.x() + b.x()) / 2 - 60, y0 - height * 0.75 - 16, f'↻ {short(label, 26)}', 8.5, True, LOOP)


# ====================================================================== C: list + jump arrows
class JumpList(Diagram):
    ROW, GUTTER, W = 50, 120, 700

    def build(self, r):
        x0 = self.GUTTER
        for i, s in enumerate(r.steps):
            y = 10 + i * self.ROW
            box = StepBox(QtCore.QRectF(x0, y, self.W, self.ROW - 8), i, radius=5)
            self.scene().addItem(box)
            self.boxes[i] = box
            self.text(x0 + 8, y + 3, f'{i + 1:>2}', 13, True, parent=box)
            self.text(x0 + 40, y + 2, short(describe_action(s), 44), 10.5, True, parent=box)
            until = describe_until(s).replace(' → next', '').replace('when done', '—')
            self.text(x0 + 40, y + 22, '⏳ ' + short(until, 110), 8.5, False, DIM, parent=box)
        lane = 0
        for i in range(len(r.steps)):
            yi = 10 + i * self.ROW + (self.ROW - 8) / 2
            for label, tgt in exits(r, i):
                if tgt in ('END', 'FAIL'):
                    dy = -10 if tgt == 'END' else 12
                    self.arrow([QtCore.QPointF(x0 + self.W, yi + dy), QtCore.QPointF(x0 + self.W + 30, yi + dy)],
                               END_C if tgt == 'END' else FAIL_C)
                    self.terminal(x0 + self.W + 32, yi + dy, tgt)
                elif isinstance(tgt, int) and tgt != i + 1:
                    lane += 1
                    lx = x0 - 14 - 18 * lane
                    yt = 10 + tgt * self.ROW + (self.ROW - 8) / 2
                    self.arrow([QtCore.QPointF(x0, yi), QtCore.QPointF(lx, yi), QtCore.QPointF(lx, yt),
                                QtCore.QPointF(x0, yt)], LOOP, dashed=tgt < i)


# ====================================================================== D: structured blocks
def loops_of(r: Routine) -> List[Tuple[int, int, str, int]]:
    """(first, last, label, step of the condition) of every backward "go to" (= a REPEAT block)."""
    out = []
    for i, s in enumerate(r.steps):
        for c in s.until:
            if c.then == 'goto' and c.goto - 1 <= i:
                out.append((c.goto - 1, i, describe_condition(c).split(' → ')[0], i))
        if s.timeout_s > 0 and s.on_timeout == 'goto' and s.timeout_goto - 1 <= i:
            out.append((s.timeout_goto - 1, i, f'timeout {s.timeout_s:g} s', i))
    merged: Dict[Tuple[int, int], List[str]] = {}
    for a, b, lab, _ in out:
        merged.setdefault((a, b), []).append(lab)
    result = []
    for (a, b), labs in merged.items():
        # no way out of the loop except a failure: it repeats forever, its condition is a normal wait
        leaves = [t for lab, t in exits(r, b) if lab not in labs and (t == 'END' or (isinstance(t, int) and t > b))]
        result.append((a, b, 'forever' if not leaves else ' or '.join(labs), b))
    return result


class Blocks(Diagram):
    ROW, W = 40, 760

    def build(self, r):
        loops = loops_of(r)
        depth = [0] * len(r.steps)
        for a, b, _, _ in loops:
            for k in range(a, b + 1):
                depth[k] += 1
        y = 10
        ys = {}
        for i, s in enumerate(r.steps):
            starts = [lp for lp in loops if lp[0] == i]
            for a, b, lab, _ in starts:
                x = 30 + 34 * (depth[i] - 1)
                guard = [c for c in r.steps[b].until if c.kind == 'count']
                times = f'  (at most {int(guard[0].value)}×)' if guard else ''
                hdr = 'REPEAT forever (until stopped or a failure)' if lab == 'forever' else f'REPEAT while {lab}{times}'
                self.text(x, y, '⟲ ' + short(hdr, 90), 10, True, LOOP)
                y += 24
            x = 30 + 34 * depth[i]
            only_loop = [lab for lab, tgt in exits(r, i) if isinstance(tgt, int) and tgt <= i
                         and not any(lp[1] == i and lp[2] == 'forever' for lp in loops)]
            waits = [lab for lab, tgt in exits(r, i) if lab != 'done' and lab not in only_loop
                     and not (lab.startswith('step entered'))]
            h = self.ROW if not waits else self.ROW + 4
            box = StepBox(QtCore.QRectF(x, y, self.W - x, h - 6), i, radius=4)
            self.scene().addItem(box)
            self.boxes[i] = box
            self.text(x + 8, y + 2, f'{i + 1}  {short(describe_action(s), 46)}', 10.5, True, parent=box)
            if waits:
                tags = []
                for lab, tgt in exits(r, i):
                    if lab in waits:
                        tags.append(lab + {'END': ' → ✔ end', 'FAIL': ' → ✖ fail'}.get(tgt, ''))
                self.text(x + 30, y + 19, '⏳ until ' + short(' | '.join(tags), 92), 8.5, False, DIM, parent=box)
            ys[i] = (y, h)
            y += h
            for a, b, lab, _ in loops:
                if b == i:
                    xa = 30 + 34 * (depth[a] - 1)
                    y_top = ys[a][0] - 24
                    pen = QtGui.QPen(LOOP, 3)
                    path = QtGui.QPainterPath(QtCore.QPointF(xa + 14, y_top + 22))
                    path.lineTo(xa + 6, y_top + 22)
                    path.lineTo(xa + 6, y - 2)
                    path.lineTo(xa + 24, y - 2)
                    self.scene().addPath(path, pen)
                    self.text(xa + 28, y - 10, 'end repeat', 8.5, False, LOOP)
                    y += 10
        self.terminal(30, y + 20, 'END')


# ====================================================================== E: structured program (proposal)
def step(bid, text, sub='', why=''):
    return {'kind': 'step', 'id': bid, 'text': text, 'sub': sub, 'why': why}


def block(kind, bid, text, body, sub='', why=''):
    return {'kind': kind, 'id': bid, 'text': text, 'sub': sub, 'body': body, 'why': why}


#: the "beam transport" program of the discussion, written with the new blocks (hand-built: the engine does
#: not know these blocks yet). Background blocks run while the main sequence runs and interrupt it.
TRANSPORT = {
    'title': 'Beam transport to the final position, HA confirmed at its maximum every 30 s',
    'background': [
        block('every', 'B1', 'EVERY 30 s', [
            step('B1a', 'Pause Pointing x, Pointing y', 'near + far field PIDs: nothing else moves'),
            block('check', 'B1b', 'CONFIRM the HA is at its maximum (extremum seeking)', [
                step('B1b1', 'Probe X: +δ  −δ  −δ  +δ around the centre',
                     'plateau = settle + mean of N frames · + − − + cancels a drift'),
                step('B1b2', 'Probe Y the same way'),
                step('B1b3', 'Gradient ± error, and curvature',
                     'sides lower than the centre? the flux value itself is never compared'),
                step('B1b4', 'Not significant and curved down  → ✔ at maximum',
                     'nothing moved except the probes (8 small moves)'),
                step('B1b5', 'Significant  → one step uphill, probe again (max 5×)',
                     'still not confirmed after 5 → see below')],
                sub='δ from the noise of the flux · never below 2 motor resolutions'),
            step('B1x', 'Not confirmed after 5  → ⚠ OPERATOR',
                 'PIDs resumed (they hold the pointing) · main sequence stays frozen · alert in Alignment: '
                 '[Continue] [Check again] [Stop]',
                 why='Nothing moves on its own: the PIDs keep the beam where it is until the operator decides.'),
            step('B1c', 'Resume Pointing x, Pointing y'),
            step('B1d', 'Wait until re-locked', 'Pointing x |err| < 0.5 · Pointing y |err| < 0.8 · 3 s · max 30 s → ✖')],
            sub='interrupts the main sequence at any moment (ramp frozen)',
            why='The main sequence (and the ramp) only goes on once the HA is confirmed at its maximum.'),
        block('every', 'B2', 'EVERY 5 min (optional)', [
            step('B2b', 'Optimise flux — 3rd mirror M3 (optimizer, one pass)',
                 'PIDs active on M1 / M2 · each measurement waits until they are in tolerance')],
            sub='does not pause the PIDs; interrupts the main sequence while it runs'),
    ],
    'main': [
        step('M1', 'Start Pointing x, Pointing y', 'coupled PIDs: near + far field each'),
        step('M2', 'Wait until locked on the current position',
             'Pointing x |err| < 0.5 · Pointing y |err| < 0.8 · 3 s · max 5 min → ✖ FAIL',
             why='= "while error > 0.5: PID" — the PIDs work, EVERY 30 s keeps optimising the HA meanwhile.'),
        block('foreach', 'F1', 'FOR EACH position in  P1 (12, 3) · P2 (20, 6) · Final (25, 8)', [
            step('M3', 'Ramp the setpoints to the position',
                 'near / far x, y at 0.2 µm/s · waits while a PID is out of its tolerance',
                 why='The setpoint moves slowly; if the beam lags (error above the tolerance of its PID) the ramp '
                     'waits for it. The HA blocks may interrupt the ramp at any moment.'),
            step('M4', 'Wait until locked on the position', 'each PID within its tolerance for 3 s · max 2 min → ✖')],
            sub='positions = setpoints near / far (µm), a table edited in the routine'),
        step('M5', 'Hold on the final position', 'until stopped — the background blocks keep running'),
    ],
}

#: a typical run: (current block, interrupted main block or None, status)
TRANSPORT_RUN = [
    ('M1', None, 'start the PIDs'), ('M2', None, 'PIDs converging (errors above their tolerance)'),
    ('B1a', 'M2', '⏱ 30 s: pause the PIDs'), ('B1b1', 'M2', '⏱ probing HA X (+ − − +)'),
    ('B1b2', 'M2', '⏱ probing HA Y'), ('B1b3', 'M2', '⏱ gradient X 0.02 ± 0.05, Y −0.01 ± 0.05: not significant'),
    ('B1b4', 'M2', '⏱ ✔ HA at its maximum'), ('B1c', 'M2', '⏱ resume PIDs'), ('B1d', 'M2', '⏱ re-locking'),
    ('M2', None, 'locked: each PID within its tolerance'),
    ('M3', None, 'ramp to P1 at 0.2 µm/s'), ('M3', None, 'ramp to P1…'),
    ('B1a', 'M3', '⏱ 30 s: ramp frozen, pause the PIDs'), ('B1b1', 'M3', '⏱ probing HA X'),
    ('B1b3', 'M3', '⏱ gradient X 0.31 ± 0.05: significant (the move shifted the top)'),
    ('B1b5', 'M3', '⏱ one step uphill on X, probe again'), ('B1b4', 'M3', '⏱ ✔ HA back at its maximum'),
    ('B1d', 'M3', '⏱ re-locking'), ('M3', None, 'ramp to P1 continues'), ('M4', None, 'locked on P1'),
    ('B1b5', 'M4', '⏱ gradient still significant after 5 steps'),
    ('B1x', 'M4', '⚠ HA maximum not confirmed: PIDs hold, waiting for the operator'),
    ('B1x', 'M4', '⚠ waiting for the operator… (he clicks "Check again")'), ('B1b4', 'M4', '⏱ ✔ HA at its maximum'),
    ('M3', None, 'ramp to P2'), ('M4', None, 'locked on P2'),
    ('B2b', 'M4', '⏱ 5 min: flux optimisation with M3, PIDs active'),
    ('M3', None, 'ramp to Final'), ('M4', None, 'locked on Final'), ('M5', None, 'holding on Final'),
    ('B1b4', 'M5', '⏱ 30 s: ✔ HA at its maximum'), ('M5', None, 'holding on Final (until stopped)')]

ICON = {'every': '⏱', 'watch': '⚠', 'foreach': '⟲', 'repeat': '⟲', 'check': '⛰'}
KIND_COLOR = {'every': LOOP, 'watch': FAIL_C, 'foreach': LOOP, 'repeat': LOOP, 'check': END_C}


class ProgramView(Diagram):
    """Structured program with background blocks (proposal). ▶ = running, ‖ = interrupted (paused)."""
    W = 800
    block_clicked = QtCore.Signal(dict)

    def __init__(self):
        super().__init__()
        self.ids: Dict[str, StepBox] = {}
        self.info: Dict[int, dict] = {}
        self.paused_markers: Dict[str, QtWidgets.QGraphicsSimpleTextItem] = {}

    def draw(self, r=None):
        self.scene().clear()
        self.boxes, self.terminals, self.ids, self.info, self.paused_markers = {}, {}, {}, {}, {}
        self.text(20, 6, TRANSPORT['title'], 12, True, TEXT)
        # two columns (1300 x 700): the main sequence on the left, what runs in the background on the right
        y = 36
        self.text(20, y, 'MAIN SEQUENCE', 10, True, DIM)
        y += 24
        for b in TRANSPORT['main']:
            y = self.draw_block(b, 40, y, 480)
        self.terminal(40, y + 14, 'END')
        y = 36
        self.text(510, y, 'IN THE BACKGROUND, while the main sequence runs (they interrupt it)', 10, True, DIM)
        y += 24
        for b in TRANSPORT['background']:
            y = self.draw_block(b, 520, y, 960)
        self.setSceneRect(self.scene().itemsBoundingRect().adjusted(-30, -20, 30, 30))

    def draw_block(self, b, x, y, right) -> float:
        if b['kind'] == 'step':
            h = 42 if b['sub'] else 28
            box = StepBox(QtCore.QRectF(x, y, right - x, h - 4), len(self.info), radius=4)
            self.info[len(self.info)] = b
            self.scene().addItem(box)
            self.ids[b['id']] = box
            self.boxes[len(self.boxes)] = box
            self.text(x + 8, y + 3, b['text'], 10.5, True, parent=box)
            if b['sub']:
                self.text(x + 26, y + 21, b['sub'], 8.5, False, DIM, parent=box)
            pm = QtWidgets.QGraphicsSimpleTextItem('‖', box)
            pm.setFont(font(13, True))
            pm.setBrush(CURRENT)
            pm.setPos(x - 18, y + 2)
            pm.setVisible(False)
            self.paused_markers[b['id']] = pm
            return y + h
        color = KIND_COLOR[b['kind']]
        head = StepBox(QtCore.QRectF(x, y, right - x, 42 if b['sub'] else 24), len(self.info),
                       fill=QtGui.QColor('#20232b'), radius=4)
        self.info[len(self.info)] = b
        self.scene().addItem(head)
        self.ids[b['id']] = head
        self.boxes[len(self.boxes)] = head
        self.text(x + 8, y + 3, f"{ICON[b['kind']]} {b['text']}", 10.5, True, color, parent=head)

        if b['sub']:
            self.text(x + 26, y + 21, b['sub'], 8.5, False, DIM, parent=head)
        y0 = y + (46 if b['sub'] else 28)
        y = y0
        for c in b['body']:
            y = self.draw_block(c, x + 30, y, right)
        pen = QtGui.QPen(color, 3, QtCore.Qt.DashLine if b['kind'] in ('every', 'watch') else QtCore.Qt.SolidLine)
        path = QtGui.QPainterPath(QtCore.QPointF(x + 14, y0 - 4))
        path.lineTo(x + 8, y0 - 4)
        path.lineTo(x + 8, y + 2)
        path.lineTo(x + 24, y + 2)
        self.scene().addPath(path, pen)
        return y + 12

    def set_marks(self, current: str, paused: Optional[str]):
        for bid, box in self.ids.items():
            box.set_current(bid == current)
        for bid, pm in self.paused_markers.items():
            pm.setVisible(bid == paused)
            if bid == paused:
                self.ids[bid].setPen(QtGui.QPen(CURRENT, 2, QtCore.Qt.DashLine))
        if current in self.ids:
            self.ensureVisible(self.ids[current], 40, 60)

    def set_current(self, step):
        pass                                           # driven by set_marks (its own scripted run)

    def _on_selection(self):
        for it in self.scene().selectedItems():
            if isinstance(it, StepBox) and it.index in self.info:
                self.block_clicked.emit(self.info[it.index])


# ====================================================================== window
LAYOUTS = (('A · Vertical flowchart', Flowchart), ('B · Horizontal timeline', Timeline),
           ('C · List + jump arrows', JumpList), ('D · Structured blocks (no go to)', Blocks))


class Window(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Routine layouts — prototype')
        self.resize(1300, 700)
        self.setStyleSheet('QWidget { background: #1d1f24; color: #f2f2f2; } '
                           'QComboBox, QPushButton { background: #2c2f38; padding: 4px 10px; font-weight: bold; } '
                           'QTabBar::tab { padding: 6px 14px; font-weight: bold; min-width: 190px; } '
                           'QTabBar::tab:selected { background: #3a3f4c; border-bottom: 3px solid #ffd75e; }')
        self.routines = {d['id']: Routine.from_dict(d) for d in demo_routines()}
        v = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel('Routine:'))
        self.combo = QtWidgets.QComboBox()
        for rid, r in self.routines.items():
            self.combo.addItem(r.name(), rid)
        self.combo.setCurrentIndex(self.combo.findData('Walk_X'))
        bar.addWidget(self.combo)
        self.btn = QtWidgets.QPushButton('▶ Simulate a run')
        bar.addWidget(self.btn)
        self.state = QtWidgets.QLabel('')
        self.state.setStyleSheet('font-weight: bold; font-size: 11pt;')
        bar.addWidget(self.state, 1)
        v.addLayout(bar)
        split = QtWidgets.QSplitter()
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.tabBar().setElideMode(QtCore.Qt.ElideNone)
        self.tabs.tabBar().setExpanding(False)
        self.views: List[Diagram] = []
        for title, cls in LAYOUTS:
            d = cls()
            d.step_clicked.connect(self.show_step)
            self.views.append(d)
            self.tabs.addTab(d, title)
        self.program = ProgramView()
        self.program.block_clicked.connect(self.show_block)
        self.tabs.addTab(self.program, 'E · Beam transport (D extended)')
        self.tabs.currentChanged.connect(lambda _: self.redraw())
        split.addWidget(self.tabs)
        self.details = QtWidgets.QTextEdit()
        self.details.setReadOnly(True)
        self.details.setStyleSheet('font-size: 10pt;')
        split.addWidget(self.details)
        split.setSizes([1000, 280])
        v.addWidget(split, 1)
        self.combo.currentIndexChanged.connect(self.redraw)
        self.btn.clicked.connect(self.toggle_run)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self.tick)
        self.run_pos = -1
        self.redraw()

    def routine(self) -> Routine:
        return self.routines[self.combo.currentData()]

    def redraw(self):
        self.timer.stop()
        self.btn.setText('▶ Simulate a run')
        self.state.setText('')
        for d in self.views:
            d.draw(self.routine())
        self.program.draw()
        self.combo.setEnabled(not self.on_program())
        self.details.setHtml('<i>Click a step to see its details (the editor would be here).</i>')

    def on_program(self) -> bool:
        return self.tabs.currentWidget() is self.program

    def show_block(self, b: dict):
        body = ''.join(f"<li>{c['text']}</li>" for c in b.get('body', []))
        self.details.setHtml(f"<h3>{ICON.get(b['kind'], '')} {b['text']}</h3><p>{b.get('sub', '')}</p>"
                             + (f'<ul>{body}</ul>' if body else '') + f"<p><i>{b.get('why', '')}</i></p>")

    def show_step(self, i: int):
        r = self.routine()
        s = r.steps[i]
        rows = ''.join(f'<li>{lab} → <b>{tgt + 1 if isinstance(tgt, int) else tgt}</b></li>' for lab, tgt in exits(r, i))
        self.details.setHtml(f'<h3>Step {i + 1}</h3><p><b>{describe_action(s)}</b></p>'
                             f'<p>{s.text}</p><p>Then:</p><ul>{rows}</ul>')

    def toggle_run(self):
        if self.timer.isActive():
            self.timer.stop()
            self.btn.setText('▶ Simulate a run')
            return
        self.run_pos = -1
        self.timer.start()
        self.btn.setText('⏹ Stop')

    def tick(self):
        if self.on_program():
            self.run_pos += 1
            if self.run_pos >= len(TRANSPORT_RUN):
                self.timer.stop()
                self.btn.setText('▶ Simulate a run')
                return
            cur, paused, status = TRANSPORT_RUN[self.run_pos]
            self.program.set_marks(cur, paused)
            self.state.setText(('‖ main sequence paused · ' if paused else '▶ ') + status)
            return
        path = RUNS.get(self.combo.currentData(), [])
        self.run_pos += 1
        if self.run_pos >= len(path):
            self.timer.stop()
            self.btn.setText('▶ Simulate a run')
            return
        step = path[self.run_pos]
        for d in self.views:
            d.set_current(step)
        txt = {'END': '✔ Finished', 'FAIL': '✖ FAILED'}.get(step) if not isinstance(step, int) else \
            f'▶ Step {step + 1}/{len(self.routine().steps)}: {describe_action(self.routine().steps[step])}'
        self.state.setText(txt)


def screenshots(out: Path):
    """Offscreen captures of every layout for every routine (to compare without running the window)."""
    w = Window()
    w.show()
    for rid in ('Walk_X', 'Keep_aligned'):
        w.combo.setCurrentIndex(w.combo.findData(rid))
        path = RUNS[rid]
        for k, (title, _) in enumerate(LAYOUTS):
            w.tabs.setCurrentIndex(k)
            for d in w.views:
                d.set_current(path[6] if len(path) > 6 else path[0])
            QtWidgets.QApplication.processEvents()
            w.grab().save(str(out / f'{rid}_{title[0]}.png'))
    w.tabs.setCurrentWidget(w.program)
    for k, idx in enumerate((5, 15)):
        cur, paused, status = TRANSPORT_RUN[idx]
        w.program.set_marks(cur, paused)
        w.state.setText(('‖ main sequence paused · ' if paused else '▶ ') + status)
        QtWidgets.QApplication.processEvents()
        w.grab().save(str(out / f'transport_E{k}.png'))


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    if '--screenshots' in sys.argv:
        target = Path(sys.argv[-1]) if len(sys.argv) > 2 else Path(__file__).parent
        screenshots(target)
        sys.exit(0)
    win = Window()
    win.show()
    sys.exit(app.exec_() if hasattr(app, 'exec_') else app.exec())
