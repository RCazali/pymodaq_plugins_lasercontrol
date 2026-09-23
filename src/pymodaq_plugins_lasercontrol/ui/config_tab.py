"""Configuration tab (T5.7): roles of this machine (T1.2), heartbeats (O9-A), safety limits of the
preset (T3.4: S1-S5 parameters), detectors acquisition, folders and version."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List, Optional

from qtpy import QtCore, QtGui, QtWidgets

from ..core.roles import sanitize_role_name
from ..core.safety import ActuatorLimits, IntensityGuardConfig, SafetyConfig
from .widgets import ask_yes_no, dspin, fmt, info, reset_rows, set_combo_items, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp

LIMIT_COLS = [('max_step', 'Pas max (S3)', 'Plus grand pas relatif d’un ordre automatique'),
              ('max_excursion', 'Excursion max (S4)', '± autour de la position d’engagement'),
              ('abs_min', 'Abs min', 'Limite absolue basse (aussi pour les mouvements manuels)'),
              ('abs_max', 'Abs max', 'Limite absolue haute (aussi pour les mouvements manuels)'),
              ('rate_window_s', 'Fenêtre (s)', 'Fenêtre glissante de la limite de débit (S5)'),
              ('rate_max', 'Débit max (S5)', 'Déplacement cumulé max sur la fenêtre'),
              ('resolution', 'Résolution', 'Plus petit pas significatif (ex. 1 pas de picomoteur)')]


def _cell_float(text: str) -> Optional[float]:
    text = (text or '').strip().replace(',', '.')
    if not text:
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


class ConfigTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self._loading = False
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        tabs = QtWidgets.QTabWidget()
        outer.addWidget(tabs)
        tabs.addTab(self._build_roles(), 'Rôles (machine)')
        tabs.addTab(self._build_safety(), 'Sécurité (preset)')
        tabs.addTab(self._build_detectors(), 'Détecteurs')
        tabs.addTab(self._build_machine(), 'Machine / dossiers')

    # ================================================================== roles
    def _build_roles(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.roles_path = QtWidgets.QLabel('')
        self.roles_path.setStyleSheet('color: gray;')
        lay.addWidget(self.roles_path)
        self.roles_table = QtWidgets.QTableWidget(0, 5)
        self.roles_table.setHorizontalHeaderLabels(['Rôle', 'Type', 'Module / canal', 'Unités', 'Note (sens, rôle…)'])
        self.roles_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.roles_table.verticalHeader().setVisible(False)
        lay.addWidget(self.roles_table, 2)
        row = QtWidgets.QHBoxLayout()
        b_add_a = styled_button('+ Rôle moteur', None)
        b_add_s = styled_button('+ Rôle signal', None)
        b_del = styled_button('Supprimer', None)
        b_auto = styled_button('Créer les rôles manquants', None,
                               'Un rôle par module et par canal 0D reçu, nommé d’après le module / canal')
        b_save = styled_button('💾 Appliquer et enregistrer', '#27ae60')
        b_reload = styled_button('↻ Recharger le fichier', None)
        for b in (b_add_a, b_add_s, b_del, b_auto):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(b_reload)
        row.addWidget(b_save)
        lay.addLayout(row)
        lay.addWidget(QtWidgets.QLabel('<b>Compteurs de trames (heartbeat, O9)</b> : un canal par détecteur, '
                                       'la donnée est périmée si le compteur n’avance plus.'))
        self.hb_table = QtWidgets.QTableWidget(0, 2)
        self.hb_table.setHorizontalHeaderLabels(['Détecteur', 'Canal compteur'])
        self.hb_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.hb_table.verticalHeader().setVisible(False)
        self.hb_table.setMaximumHeight(140)
        lay.addWidget(self.hb_table, 1)
        b_add_a.clicked.connect(lambda: self._add_role_row('actionneur'))
        b_add_s.clicked.connect(lambda: self._add_role_row('signal'))
        b_del.clicked.connect(self._del_role_row)
        b_auto.clicked.connect(self._auto_roles)
        b_save.clicked.connect(self._save_roles)
        b_reload.clicked.connect(self._reload_roles)
        return w

    def _target_combo(self, kind: str, current: str) -> QtWidgets.QComboBox:
        c = QtWidgets.QComboBox()
        c.setEditable(True)
        items = self.app.core.broker.modules() if kind == 'actionneur' else self.app.core.signals.channels()
        set_combo_items(c, [''] + sorted(items), current)
        c.setFocusPolicy(QtCore.Qt.ClickFocus)
        return c

    def _add_role_row(self, kind: str, name: str = '', target: str = '', units: str = '', note: str = ''):
        r = self.roles_table.rowCount()
        self.roles_table.insertRow(r)
        self.roles_table.setItem(r, 0, QtWidgets.QTableWidgetItem(name))
        k = QtWidgets.QTableWidgetItem(kind)
        k.setFlags(QtCore.Qt.ItemIsEnabled)
        self.roles_table.setItem(r, 1, k)
        self.roles_table.setCellWidget(r, 2, self._target_combo(kind, target))
        self.roles_table.setItem(r, 3, QtWidgets.QTableWidgetItem(units))
        self.roles_table.setItem(r, 4, QtWidgets.QTableWidgetItem(note))

    def _del_role_row(self):
        r = self.roles_table.currentRow()
        if r >= 0:
            self.roles_table.removeRow(r)

    def load_roles(self):
        roles = self.app.core.roles
        self.roles_path.setText(f'Fichier : {roles.path}  (machine « {self.app.machine} »)')
        self.roles_table.setRowCount(0)
        for r in sorted(roles.actuators.values(), key=lambda x: x.name):
            self._add_role_row('actionneur', r.name, r.module, r.units, r.note)
        for r in sorted(roles.signals.values(), key=lambda x: x.name):
            self._add_role_row('signal', r.name, r.channel, r.units, r.note)
        dets = sorted(set(self.app.core.broker.detectors) | set(roles.heartbeats) |
                      set(self.app.core.signals.detectors()))
        reset_rows(self.hb_table, len(dets))
        for i, d in enumerate(dets):
            it = QtWidgets.QTableWidgetItem(d)
            it.setFlags(QtCore.Qt.ItemIsEnabled)
            self.hb_table.setItem(i, 0, it)
            c = QtWidgets.QComboBox()
            chans = [ch for ch in self.app.core.signals.channels() if self.app.core.signals.detector_of(ch) == d]
            set_combo_items(c, [''] + chans, roles.heartbeats.get(d, ''))
            c.setEditable(True)
            self.hb_table.setCellWidget(i, 1, c)

    def _table_roles(self):
        from ..core.roles import RoleMap
        rm = RoleMap(machine=self.app.machine, description=self.app.core.roles.description,
                     path=self.app.core.roles.path)
        errors = []
        for r in range(self.roles_table.rowCount()):
            name = sanitize_role_name((self.roles_table.item(r, 0).text() if self.roles_table.item(r, 0) else ''))
            kind = self.roles_table.item(r, 1).text()
            target = self.roles_table.cellWidget(r, 2).currentText().strip()
            units = self.roles_table.item(r, 3).text() if self.roles_table.item(r, 3) else ''
            note = self.roles_table.item(r, 4).text() if self.roles_table.item(r, 4) else ''
            if not target:
                errors.append(f'ligne {r + 1} ({name}) : cible vide')
                continue
            if name in rm.all_roles():
                errors.append(f'rôle en double : {name}')
                continue
            if kind == 'actionneur':
                rm.set_actuator(name, target, units, note)
            else:
                rm.set_signal(name, target, units, note)
        for i in range(self.hb_table.rowCount()):
            d = self.hb_table.item(i, 0).text()
            ch = self.hb_table.cellWidget(i, 1).currentText().strip()
            if ch:
                rm.heartbeats[d] = ch
        return rm, errors

    def _save_roles(self):
        rm, errors = self._table_roles()
        if errors:
            warn(self, 'Rôles', 'Corriger :\n' + '\n'.join(errors))
            return
        self.app.apply_roles(rm, save=True)          # reloads this tab (set_hardware)

    def _reload_roles(self):
        self.app.reload_roles()

    def _auto_roles(self):
        rm, _ = self._table_roles()
        created = rm.auto_complete(self.app.core.broker.modules(), self.app.core.signals.channels(),
                                   units_of=self.app.core.broker.units)
        if not created:
            info(self, 'Rôles', 'Aucun rôle manquant (modules et canaux reçus déjà couverts).')
            return
        self.app.apply_roles(rm, save=False)
        info(self, 'Rôles', f'{len(created)} rôle(s) créé(s). Vérifier puis « Appliquer et enregistrer ».')

    # ================================================================== safety
    def _build_safety(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        form = QtWidgets.QFormLayout()
        self.max_age = dspin(1.0, 0.05, 3600, 2, tip='S1 : au-delà, la donnée est périmée et les automatismes gelés')
        self.timeout = dspin(20.0, 0.5, 3600, 1, tip='T3.1 : un mouvement automatique plus long gèle l’automatisme')
        self.manual_limits = QtWidgets.QCheckBox('appliquer abs_min / abs_max aux mouvements manuels')
        form.addRow('Âge max des données (s) :', self.max_age)
        form.addRow('Délai max d’un mouvement (s) :', self.timeout)
        form.addRow('', self.manual_limits)
        lay.addLayout(form)
        g = QtWidgets.QGroupBox('Garde intensité (S2)')
        gf = QtWidgets.QGridLayout(g)
        self.g_enabled = QtWidgets.QCheckBox('activée')
        self.g_role = QtWidgets.QComboBox()
        self.g_mode = QtWidgets.QComboBox()
        self.g_mode.addItems(['relative', 'absolute'])
        self.g_threshold = dspin(0.5, 0, 1e12, 4, tip='relatif : fraction de la référence ; absolu : valeur')
        self.g_hyst = dspin(0.1, 0, 1e12, 4, tip='ajouté au seuil pour considérer l’intensité revenue')
        self.g_fixed = QtWidgets.QCheckBox('référence fixe :')
        self.g_ref = dspin(1.0, -1e12, 1e12, 6)
        self.g_capture = styled_button('📍 Capturer la référence', None,
                                       'Référence = valeur actuelle (mode relatif, référence non fixe)')
        self.g_info = QtWidgets.QLabel('')
        gf.addWidget(self.g_enabled, 0, 0)
        gf.addWidget(QtWidgets.QLabel('Rôle :'), 0, 1)
        gf.addWidget(self.g_role, 0, 2)
        gf.addWidget(QtWidgets.QLabel('Mode :'), 0, 3)
        gf.addWidget(self.g_mode, 0, 4)
        gf.addWidget(QtWidgets.QLabel('Seuil :'), 1, 1)
        gf.addWidget(self.g_threshold, 1, 2)
        gf.addWidget(QtWidgets.QLabel('Hystérésis :'), 1, 3)
        gf.addWidget(self.g_hyst, 1, 4)
        gf.addWidget(self.g_fixed, 2, 1)
        gf.addWidget(self.g_ref, 2, 2)
        gf.addWidget(self.g_capture, 2, 3, 1, 2)
        gf.addWidget(self.g_info, 3, 0, 1, 5)
        lay.addWidget(g)
        lay.addWidget(QtWidgets.QLabel('<b>Limites par rôle moteur</b> (cellule vide = non définie ; '
                                       'pas max, excursion max et débit max sont obligatoires pour piloter '
                                       'automatiquement un moteur)'))
        self.limits_table = QtWidgets.QTableWidget(0, len(LIMIT_COLS) + 1)
        self.limits_table.setHorizontalHeaderLabels(['Rôle'] + [c[1] for c in LIMIT_COLS])
        for i, c in enumerate(LIMIT_COLS):
            self.limits_table.horizontalHeaderItem(i + 1).setToolTip(c[2])
        self.limits_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.limits_table.verticalHeader().setVisible(False)
        lay.addWidget(self.limits_table, 1)
        for sig in (self.max_age.valueChanged, self.timeout.valueChanged, self.g_threshold.valueChanged,
                    self.g_hyst.valueChanged, self.g_ref.valueChanged):
            sig.connect(self._on_safety_changed)
        for sig in (self.manual_limits.toggled, self.g_enabled.toggled, self.g_fixed.toggled):
            sig.connect(self._on_safety_changed)
        self.g_role.currentTextChanged.connect(self._on_safety_changed)
        self.g_mode.currentTextChanged.connect(self._on_safety_changed)
        self.limits_table.itemChanged.connect(self._on_safety_changed)
        self.g_capture.clicked.connect(self._capture_reference)
        return w

    def load_safety(self, safety: SafetyConfig):
        self._loading = True
        try:
            self.max_age.setValue(safety.max_data_age_s)
            self.timeout.setValue(safety.move_timeout_s)
            self.manual_limits.setChecked(safety.manual_abs_limits)
            g = safety.intensity
            self.g_enabled.setChecked(g.enabled)
            set_combo_items(self.g_role, [''] + sorted(self.app.core.roles.signals), g.role or '')
            self.g_mode.setCurrentText(g.mode)
            self.g_threshold.setValue(g.threshold)
            self.g_hyst.setValue(g.hysteresis)
            self.g_fixed.setChecked(g.reference is not None)
            if g.reference is not None:
                self.g_ref.setValue(g.reference)
            roles = sorted(self.app.core.roles.actuators)
            roles += [r for r in safety.actuators if r not in roles]
            self.limits_table.setRowCount(len(roles))
            for i, role in enumerate(roles):
                it = QtWidgets.QTableWidgetItem(role if self.app.core.roles.is_actuator(role) else f'⚠ {role}')
                it.setData(QtCore.Qt.UserRole, role)
                it.setFlags(QtCore.Qt.ItemIsEnabled)
                self.limits_table.setItem(i, 0, it)
                lim = safety.actuators.get(role, ActuatorLimits())
                for j, (key, _, _) in enumerate(LIMIT_COLS):
                    v = getattr(lim, key)
                    text = '' if v is None else f'{v:g}'
                    if key == 'rate_window_s' and role not in safety.actuators:
                        text = '60'
                    if key == 'resolution' and not v:
                        text = ''
                    cell = QtWidgets.QTableWidgetItem(text)
                    if key in ('max_step', 'max_excursion', 'rate_max') and v is None:
                        cell.setBackground(QtGui.QColor(231, 76, 60, 60))
                    self.limits_table.setItem(i, j + 1, cell)
        finally:
            self._loading = False
        self._update_guard_info()

    def safety_config(self) -> SafetyConfig:
        g = IntensityGuardConfig(enabled=self.g_enabled.isChecked(), role=self.g_role.currentText() or None,
                                 mode=self.g_mode.currentText(), threshold=self.g_threshold.value(),
                                 hysteresis=self.g_hyst.value(),
                                 reference=self.g_ref.value() if self.g_fixed.isChecked() else None)
        acts: Dict[str, ActuatorLimits] = {}
        for i in range(self.limits_table.rowCount()):
            role = self.limits_table.item(i, 0).data(QtCore.Qt.UserRole)
            vals = {}
            for j, (key, _, _) in enumerate(LIMIT_COLS):
                it = self.limits_table.item(i, j + 1)
                vals[key] = _cell_float(it.text() if it else '')
            if all(vals[k] is None for k in ('max_step', 'max_excursion', 'abs_min', 'abs_max', 'rate_max')):
                continue
            acts[role] = ActuatorLimits.from_dict(vals)
        return SafetyConfig(max_data_age_s=self.max_age.value(), move_timeout_s=self.timeout.value(),
                            manual_abs_limits=self.manual_limits.isChecked(), intensity=g, actuators=acts)

    def _on_safety_changed(self, *args):
        if self._loading:
            return
        self._loading = True
        try:
            for i in range(self.limits_table.rowCount()):
                for j, (key, _, _) in enumerate(LIMIT_COLS):
                    it = self.limits_table.item(i, j + 1)
                    if it is None:
                        continue
                    missing = key in ('max_step', 'max_excursion', 'rate_max') and _cell_float(it.text()) is None
                    bad = it.text().strip() != '' and _cell_float(it.text()) is None
                    it.setBackground(QtGui.QColor(231, 76, 60, 60) if (missing or bad) else QtGui.QBrush())
        finally:
            self._loading = False
        self.app.apply_safety(self.safety_config())
        self._update_guard_info()

    def _capture_reference(self):
        if self.app.core.safety.guard.capture_reference():
            self.app.core.events.publish('safety.info', 'SAFETY',
                                         f"Référence d'intensité capturée : {self.app.core.safety.guard.reference:.4g}")
        else:
            warn(self, 'Garde intensité', 'Capture impossible (mode absolu, signal absent ou périmé).')
        self._update_guard_info()

    def _update_guard_info(self):
        g = self.app.core.safety.guard
        trip, release = g.thresholds()
        self.g_info.setText(f'état : {g.evaluate()} · valeur : {fmt(g.value())} · référence : {fmt(g.reference)} · '
                            f'seuil : {fmt(trip)} · reprise : {fmt(release)}')

    def write_to_preset(self, preset: dict):
        preset['safety'] = self.safety_config().to_dict()

    # ================================================================== detectors
    def _build_detectors(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.det_table = QtWidgets.QTableWidget(0, 5)
        self.det_table.setHorizontalHeaderLabels(['Détecteur', 'Initialisé', 'Acquisition', 'Fraîcheur', 'Fréquence'])
        self.det_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.det_table.verticalHeader().setVisible(False)
        self.det_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.det_table)
        row = QtWidgets.QHBoxLayout()
        b_start = styled_button('▶ Lancer l’acquisition (sélection)', '#2980b9')
        b_stop = styled_button('⏹ Arrêter l’acquisition (sélection)', None)
        b_all = styled_button('▶ Tous les détecteurs des rôles', None)
        b_start.clicked.connect(lambda: self.app.core.broker.start_grab(self._selected_dets()))
        b_stop.clicked.connect(lambda: self.app.core.broker.stop_grab(self._selected_dets()))
        b_all.clicked.connect(self.app.start_detectors)
        row.addWidget(b_start)
        row.addWidget(b_stop)
        row.addWidget(b_all)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(QtWidgets.QLabel('<i>Les automatismes ne déclenchent jamais d’acquisition : ils lisent la '
                                       'dernière valeur reçue (T3.2). Le viewer LECO de l’ordi B doit être en '
                                       'acquisition continue.</i>'))
        lay.addWidget(QtWidgets.QLabel('<b>Canaux 0D reçus</b>'))
        self.chan_table = QtWidgets.QTableWidget(0, 4)
        self.chan_table.setHorizontalHeaderLabels(['Canal', 'Rôle', 'Valeur', 'Âge (s)'])
        self.chan_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.chan_table.verticalHeader().setVisible(False)
        self.chan_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.chan_table, 1)
        return w

    def _selected_dets(self) -> List[str]:
        rows = {i.row() for i in self.det_table.selectedIndexes()}
        return [self.det_table.item(r, 0).text() for r in sorted(rows)]

    def refresh_detectors(self):
        core = self.app.core
        dets = sorted(core.broker.detectors)
        if self.det_table.rowCount() != len(dets):
            self.det_table.setRowCount(len(dets))
        for i, d in enumerate(dets):
            st = core.signals.detector_status(d)
            port = core.broker.detectors[d]
            vals = [d, 'oui' if port.is_initialized() else 'non', 'en cours' if core.broker.is_grabbing(d) else 'arrêtée',
                    f'{st.state} {st.reason}', f'{st.rate_hz:.2f} Hz' if st.rate_hz else '—']
            for j, v in enumerate(vals):
                it = self.det_table.item(i, j)
                if it is None:
                    it = QtWidgets.QTableWidgetItem()
                    self.det_table.setItem(i, j, it)
                it.setText(v)
        chans = core.signals.channels()
        if self.chan_table.rowCount() != len(chans):
            self.chan_table.setRowCount(len(chans))
        for i, ch in enumerate(chans):
            role = core.roles.role_for_channel(ch) or ''
            vals = [ch, role, fmt(core.signals.value(ch), 6), f'{core.signals.age(ch):.1f}']
            for j, v in enumerate(vals):
                it = self.chan_table.item(i, j)
                if it is None:
                    it = QtWidgets.QTableWidgetItem()
                    self.chan_table.setItem(i, j, it)
                it.setText(v)

    # ================================================================== machine
    def _build_machine(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(w)
        p = self.app.paths
        form.addRow('Version :', QtWidgets.QLabel(f'{self.app.version}  (git {self.app.git_hash})'))
        form.addRow('Machine :', QtWidgets.QLabel(self.app.machine))
        self.operator = QtWidgets.QLineEdit(self.app.operator)
        self.operator.editingFinished.connect(lambda: self.app.set_operator(self.operator.text().strip()))
        form.addRow('Opérateur :', self.operator)
        for label, path in (('Données :', p.data), ('Rôles :', p.roles_file), ('Presets :', p.presets),
                            ('Enregistrements :', p.recordings), ('Journal technique :', p.logs),
                            ('Configuration :', self.app.config_file_path())):
            row = QtWidgets.QHBoxLayout()
            lbl = QtWidgets.QLabel(str(path))
            lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            row.addWidget(lbl, 1)
            b = styled_button('📂', None, 'ouvrir le dossier', bold=False)
            target = path if path is not None and path.suffix == '' else (path.parent if path else None)
            b.clicked.connect(lambda _=False, t=target: t and QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(t))))
            row.addWidget(b)
            form.addRow(label, row)
        form.addRow(QtWidgets.QLabel('<i>Les dossiers se règlent dans le fichier de configuration du plugin '
                                     '(config_lasercontrol.toml, un par machine, hors git).</i>'))
        return w

    # ================================================================== preset / refresh
    def load_from_preset(self, preset: dict):
        self.load_roles()
        self.load_safety(SafetyConfig.from_dict(preset.get('safety', {})))

    def set_hardware(self):
        self.load_roles()
        self.load_safety(self.app.core.safety.config)     # the core is the reference

    def refresh(self):
        self.refresh_detectors()
        self._update_guard_info()
