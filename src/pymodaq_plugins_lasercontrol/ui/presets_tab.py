"""Presets tab (L6): one versioned file per preset, validated presets first, validation against
this machine (T6.2), 'mark as validated' with notes (T6.4), import of the historical profiles (T6.3).
Loading a preset never starts anything (S9)."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from qtpy import QtCore, QtGui, QtWidgets

from ..presets import LegacyImport, content_hash, validate_preset
from .widgets import Badge, ask_yes_no, info, styled_button, warn

if TYPE_CHECKING:  # pragma: no cover
    from .app import LaserControlApp


class PresetsTab(QtWidgets.QWidget):
    def __init__(self, app: 'LaserControlApp', parent=None):
        super().__init__(parent)
        self.app = app
        self._loading = False
        main = QtWidgets.QHBoxLayout(self)
        main.setContentsMargins(6, 6, 6, 6)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel('<b>Presets</b> (validés en premier)'))
        self.list = QtWidgets.QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self._on_load())
        left.addWidget(self.list, 1)
        grid = QtWidgets.QGridLayout()
        self.btn_load = styled_button('📥 Charger', '#2980b9', 'Charge le preset (rien ne démarre)')
        self.btn_new = styled_button('✚ Nouveau', None)
        self.btn_delete = styled_button('🗑 Supprimer', None)
        self.btn_import = styled_button('⇪ Importer un ancien profil…', None,
                                        'Profils PID / optimisation / XY / session de l’ancienne extension')
        self.btn_folder = styled_button('📂 Dossier', None)
        grid.addWidget(self.btn_load, 0, 0)
        grid.addWidget(self.btn_new, 0, 1)
        grid.addWidget(self.btn_delete, 1, 0)
        grid.addWidget(self.btn_folder, 1, 1)
        grid.addWidget(self.btn_import, 2, 0, 1, 2)
        left.addLayout(grid)
        main.addLayout(left, 1)

        right = QtWidgets.QVBoxLayout()
        head = QtWidgets.QHBoxLayout()
        self.name = QtWidgets.QLabel('—')
        self.name.setStyleSheet('font-size: 13pt; font-weight: bold;')
        head.addWidget(self.name)
        self.badge = Badge('brouillon', '#e67e22')
        head.addWidget(self.badge)
        head.addStretch()
        self.hash = QtWidgets.QLabel('')
        self.hash.setStyleSheet('color: gray;')
        head.addWidget(self.hash)
        right.addLayout(head)
        form = QtWidgets.QFormLayout()
        self.description = QtWidgets.QPlainTextEdit()
        self.description.setMaximumHeight(70)
        form.addRow('Description :', self.description)
        self.regime = QtWidgets.QComboBox()
        self.regime.setEditable(True)
        self.regime.addItems(['CW', 'ML', 'autre'])
        form.addRow('Régime :', self.regime)
        self.pump = QtWidgets.QLineEdit()
        self.pump.setPlaceholderText('ex. 180 W')
        form.addRow('Puissance de pompe :', self.pump)
        self.cond_notes = QtWidgets.QLineEdit()
        form.addRow('Conditions / notes :', self.cond_notes)
        self.validation = QtWidgets.QLabel('')
        self.validation.setWordWrap(True)
        self.validation.setStyleSheet('color: gray;')
        form.addRow('Validation :', self.validation)
        right.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.btn_save = styled_button('💾 Enregistrer', '#27ae60')
        self.btn_save_as = styled_button('💾 Enregistrer sous…', None)
        self.btn_validate = styled_button('✔ Marquer comme validé…', '#16a085',
                                          'Après un essai réussi : auteur + commentaire (T6.4)')
        self.btn_check = styled_button('🔎 Vérifier sur cette machine', None)
        for b in (self.btn_save, self.btn_save_as, self.btn_validate, self.btn_check):
            row.addWidget(b)
        right.addLayout(row)
        right.addWidget(QtWidgets.QLabel('<b>Cohérence avec cette machine</b> (rôles, modules, limites de sécurité) :'))
        self.report = QtWidgets.QPlainTextEdit()
        self.report.setReadOnly(True)
        right.addWidget(self.report, 1)
        main.addLayout(right, 2)

        self.btn_load.clicked.connect(self._on_load)
        self.btn_new.clicked.connect(self._on_new)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_import.clicked.connect(self._on_import)
        self.btn_folder.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(self.app.paths.presets))))
        self.btn_save.clicked.connect(lambda: self.app.save_preset())
        self.btn_save_as.clicked.connect(self._on_save_as)
        self.btn_validate.clicked.connect(self._on_validate)
        self.btn_check.clicked.connect(self.check)
        for w in (self.description, ):
            w.textChanged.connect(self._on_changed)
        self.regime.currentTextChanged.connect(self._on_changed)
        self.pump.textChanged.connect(self._on_changed)
        self.cond_notes.textChanged.connect(self._on_changed)
        self.refresh_list()

    # ------------------------------------------------------------------ list
    def refresh_list(self):
        self.list.clear()
        for inf in self.app.store.list():
            it = QtWidgets.QListWidgetItem(('✔ ' if inf.status == 'validated' else '   ') + inf.name)
            it.setData(QtCore.Qt.UserRole, inf.name)
            tip = f'{inf.description}\nrégime : {inf.regime}\nmodifié : {inf.modified}'
            if inf.validated_on:
                tip += f'\nvalidé : {inf.validated_on}'
            it.setToolTip(tip)
            if inf.name == self.app.preset.get('name'):
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.list.addItem(it)

    def _selected(self):
        it = self.list.currentItem()
        return it.data(QtCore.Qt.UserRole) if it else None

    # ------------------------------------------------------------------ preset <-> UI
    def load_from_preset(self, preset: dict):
        self._loading = True
        try:
            self.name.setText(preset.get('name', '—'))
            self.description.setPlainText(preset.get('description', ''))
            c = preset.get('conditions', {})
            self.regime.setCurrentText(c.get('regime', 'CW'))
            self.pump.setText(str(c.get('pump_power', '')))
            self.cond_notes.setText(c.get('notes', ''))
        finally:
            self._loading = False
        self._update_status(preset)
        self.refresh_list()
        self.check()

    def write_to_preset(self, preset: dict):
        preset['description'] = self.description.toPlainText()
        preset['conditions'] = {'regime': self.regime.currentText(), 'pump_power': self.pump.text(),
                                'notes': self.cond_notes.text()}

    def _update_status(self, preset: dict):
        if preset.get('status') == 'validated':
            self.badge.set('✔ validé', '#27ae60')
            self.validation.setText(f"validé le {preset.get('validated_on')} par {preset.get('validated_by') or '?'}"
                                    f"\n{preset.get('validation_notes', '')}")
        else:
            self.badge.set('brouillon', '#e67e22')
            self.validation.setText(preset.get('validation_notes', '') or 'non validé')
        self.hash.setText(f"empreinte {content_hash(preset)}")

    def _on_changed(self, *args):
        if not self._loading:
            self.app.preset_dirty = True

    def check(self):
        p = self.app.collect_preset()
        rep = validate_preset(p, self.app.core.roles, self.app.core.broker.modules(), self.app.core.signals.channels())
        self.report.setPlainText(rep.text())

    # ------------------------------------------------------------------ actions
    def _on_load(self):
        name = self._selected()
        if name:
            self.app.load_preset_interactive(name, self)

    def _on_new(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'Nouveau preset', 'Nom :')
        if ok and name.strip():
            self.app.new_preset(name.strip(), self)

    def _on_delete(self):
        name = self._selected()
        if name and ask_yes_no(self, 'Supprimer', f'Supprimer le fichier du preset « {name} » ?'):
            self.app.store.delete(name)
            self.refresh_list()

    def _on_save_as(self):
        name, ok = QtWidgets.QInputDialog.getText(self, 'Enregistrer sous', 'Nom du preset :',
                                                  text=self.app.preset.get('name', ''))
        if ok and name.strip():
            if self.app.store.exists(name.strip()) and name.strip() != self.app.preset.get('name') and \
                    not ask_yes_no(self, 'Remplacer', f'« {name.strip()} » existe déjà. Remplacer ?'):
                return
            self.app.save_preset(name.strip())

    def _on_validate(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle('Marquer comme validé')
        form = QtWidgets.QFormLayout(dlg)
        who = QtWidgets.QLineEdit(self.app.operator)
        notes = QtWidgets.QPlainTextEdit()
        notes.setPlaceholderText('conditions de l’essai, résultat, remarques…')
        form.addRow('Validé par :', who)
        form.addRow('Commentaire :', notes)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        fn = getattr(dlg, 'exec', None) or dlg.exec_
        if fn() == QtWidgets.QDialog.Accepted:
            self.app.validate_current_preset(who.text().strip(), notes.toPlainText().strip())

    def _on_import(self):
        start = str(Path.home() / '.pymodaq')
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, 'Anciens profils (JSON)', start, 'JSON (*.json)')
        if paths:
            self.app.import_legacy(paths, self)

    def refresh(self):
        self._update_status(self.app.preset)
        dirty = ' *' if self.app.preset_dirty else ''
        self.name.setText(self.app.preset.get('name', '—') + dirty)
