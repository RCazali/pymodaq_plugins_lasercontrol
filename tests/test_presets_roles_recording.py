"""Roles (T1.2), presets (T6.1-T6.4), legacy import (T6.3), recording (T7.x)."""
import json
import math
import time
from pathlib import Path

import numpy as np
import pytest

from pymodaq_plugins_lasercontrol.core import RoleMap, AutoState
from pymodaq_plugins_lasercontrol.core.signals import SignalSource
from pymodaq_plugins_lasercontrol.core.clock import FakeClock
from pymodaq_plugins_lasercontrol.presets import (PresetStore, default_preset, validate_preset, content_hash,
                                                  normalize, LegacyImport, roles_used)
from pymodaq_plugins_lasercontrol.recording import (Recorder, load_session, KIND_PRE, KIND_BURST, KIND_PERIODIC,
                                                    EventTriggerWriter, build_snapshot)

from conftest import Rig


# --------------------------------------------------------------------------- roles
def test_roles_toml_roundtrip(tmp_path):
    rm = RoleMap(machine='lab', description='ordi A')
    rm.set_actuator('OC_H', 'Pico OC H', units='step', note='d = droite')
    rm.set_signal('Cam1_x', 'LECO_B/Cam1/x', units='px')
    rm.heartbeats = {'LECO_B': 'LECO_B/Stats/frame_counter'}
    p = rm.save(tmp_path / 'roles_lab.toml')
    back = RoleMap.load(p)
    assert back.module_for('OC_H') == 'Pico OC H'
    assert back.channel_for('Cam1_x') == 'LECO_B/Cam1/x'
    assert back.heartbeats == {'LECO_B': 'LECO_B/Stats/frame_counter'}
    assert back.actuators['OC_H'].note == 'd = droite'


def test_roles_auto_complete():
    rm = RoleMap()
    created = rm.auto_complete(['Pico OC/H', 'Stage'], ['Det/Mock0D/Mock_00', 'Det/Mock0D/Mock_01',
                                                      'Det2/Mock0D/Mock_00'])
    assert rm.module_for('Pico_OC_H') == 'Pico OC/H'
    assert len(created) == 5
    assert rm.channel_for('Det2_Mock0D_Mock_00') == 'Det2/Mock0D/Mock_00'


def test_signal_legacy_alias_resolves_to_first_channel():
    s = SignalSource(FakeClock(), max_age_s=1.0)
    s.ingest('Det', {'Det/Mock0D/Mock_00': 1.5, 'Det/Mock0D/Mock_01': 2.5})
    assert s.value('Det/Mock0D') == 1.5          # historical key 'origin/name' = first channel


# --------------------------------------------------------------------------- presets
def test_preset_store_roundtrip_and_validation_mark(tmp_path):
    store = PresetStore(tmp_path)
    p = default_preset('Stab CW')
    p['pids'].append({'id': 'H', 'signal': 'Cam1_x', 'motors': {'OC_H': 1.0}, 'settings': {'kp': 0.2}})
    store.save(p)
    q = store.load('Stab CW')
    assert q['pids'][0]['settings']['kp'] == 0.2
    assert q['pids'][0]['settings']['period_s'] == 0.5
    store.mark_validated(q, by='romain', notes='OK en CW')
    store.save(q)
    assert store.list()[0].status == 'validated'
    q['pids'][0]['settings']['kp'] = 0.3            # content change -> back to draft
    store.save(q)
    assert store.load('Stab CW')['status'] == 'draft'


def test_validated_presets_listed_first(tmp_path):
    store = PresetStore(tmp_path)
    a = default_preset('a_draft')
    b = default_preset('z_valid')
    store.mark_validated(b)
    store.save(a)
    store.save(b)
    assert [i.name for i in store.list()] == ['z_valid', 'a_draft']


def test_content_hash_ignores_metadata():
    p = default_preset('x')
    h = content_hash(p)
    p['description'] = 'autre'
    p['modified'] = 'later'
    assert content_hash(p) == h
    p['safety']['max_data_age_s'] = 3.0
    assert content_hash(p) != h


def test_validate_preset_reports_per_automation():
    rm = RoleMap()
    rm.set_actuator('OC_H', 'modH')
    rm.set_signal('Cam1_x', 'B/Cam1/x')
    rm.set_signal('I', 'B/Laser/power')
    p = default_preset('v')
    p['safety']['intensity']['role'] = 'I'
    p['pids'] = [{'id': 'ok', 'signal': 'Cam1_x', 'motors': {'OC_H': 1.0}},
                 {'id': 'bad', 'signal': 'Cam2_x', 'motors': {'EM_H': 1.0}}]
    p['safety']['actuators'] = {'OC_H': {'max_step': 1, 'max_excursion': 10, 'rate_max': 50}}
    rep = validate_preset(normalize(p), rm, modules=['modH'], channels=['B/Cam1/x', 'B/Laser/power'])
    assert rep.automations['PID:ok'] == []
    bad = ' '.join(rep.automations['PID:bad'])
    assert 'Cam2_x' in bad and 'EM_H' in bad
    assert roles_used(normalize(p))['actuators'] == ['EM_H', 'OC_H']


def test_same_preset_on_two_machines():
    p = default_preset('portable')
    p['safety']['intensity']['role'] = 'I'
    p['pids'] = [{'id': 'H', 'signal': 'X', 'motors': {'OC_H': 1.0}}]
    p['safety']['actuators'] = {'OC_H': {'max_step': 1, 'max_excursion': 10, 'rate_max': 50}}
    p = normalize(p)
    lab = RoleMap(machine='lab')
    lab.set_actuator('OC_H', 'Pico_OC_H')
    lab.set_signal('X', 'LECO_B/Cam1/x')
    lab.set_signal('I', 'Power/P/CH00')
    mock = RoleMap(machine='mock')
    mock.set_actuator('OC_H', 'LaserSim OC_H')
    mock.set_signal('X', 'SimB/Cam1/x')
    mock.set_signal('I', 'SimB/Laser/power')
    assert validate_preset(p, lab, ['Pico_OC_H'], ['LECO_B/Cam1/x', 'Power/P/CH00']).ok
    assert validate_preset(p, mock, ['LaserSim OC_H'], ['SimB/Cam1/x', 'SimB/Laser/power']).ok


# --------------------------------------------------------------------------- legacy import
def test_import_legacy_pid_profile(tmp_path):
    old = {'PID_Main': {'pid_id': 'PID_Main', 'detector': 'LECO_B/Cam1',
                        'weights': {'Pico_OC_H': 1.0, 'Pico_EM_H': -0.5},
                        'limits': {'Pico_OC_H': [-100, 100], 'Pico_EM_H': [-50, 80]},
                        'config': {'setpoint': 12.0, 'kp': 0.1, 'sign_kp': '-', 'ki': 0.01, 'sign_ki': '+',
                                   'kd': 0.0, 'sign_kd': '+', 'max_step': 2.0, 'sample_rate': 0.5,
                                   'confirm_before_move': False}},
           'PID_V': {'pid_id': 'PID_V', 'detector': 'LECO_B/Cam1y', 'weights': {'Pico_OC_H': 1.0},
                     'limits': {'Pico_OC_H': [-80, 120]}, 'config': {'kp': 0.2}}}
    path = tmp_path / 'pid_profile_daily.json'
    path.write_text(json.dumps(old))
    rm = RoleMap()
    rm.set_actuator('OC_H', 'Pico_OC_H')                       # existing role reused
    imp = LegacyImport(rm)
    p = imp.import_file(path)
    pid = p['pids'][0]
    assert pid['motors'] == {'OC_H': 1.0, 'Pico_EM_H': -0.5}
    assert pid['settings']['sign_kp'] == '-' and pid['settings']['period_s'] == 0.5
    lim = p['safety']['actuators']['OC_H']
    assert lim['abs_min'] == -80 and lim['abs_max'] == 100          # most restrictive
    assert lim['max_step'] is None                                  # never invented
    assert rm.channel_for(pid['signal']) == 'LECO_B/Cam1'


def test_import_legacy_opt_and_xy(tmp_path):
    opt = {'opt_config': {'interval_min': 2.0, 'run_mode': 'Nelder-Mead (simplex)'},
           'active_motors': [{'name': 'Jet_X', 'max_step': 0.1, 'probe_step': 0.02, 'order': 0}],
           'target_signals': [{'channel': 'XUV/flux', 'mode': 'Maximize', 'weight': 1.0, 'p': 2.0,
                               'target_value': 0.0, 'auto_target': True}]}
    (tmp_path / 'opt_profile_a.json').write_text(json.dumps(opt))
    xy = {'tabs': [{'pair_id': 't1', 'tab_title': 'Jet', 'actuator_x': 'Jet_X', 'actuator_y': 'Jet_Y',
                    'step_x': 1, 'step_y': 1, 'limits': {'xmin': -5, 'xmax': 5, 'ymin': -5, 'ymax': 5},
                    'saved_positions': {'home': {'x': 0, 'y': 0}}}],
          'alignment': {'axes': [{'name': 'OC', 'h': {'actuator': 'Pico_OC_H', 'invert': True, 'mode': 'auto',
                                                      'slow': 100, 'fast': 1000, 'min': 0, 'max': 25,
                                                      'speed_param': '', 'label': 'Horiz', 'decimals': 0},
                                  'v': None}], 'active': 0}}
    (tmp_path / 'Default.json').write_text(json.dumps(xy))
    rm = RoleMap()
    imp = LegacyImport(rm)
    p = imp.import_file(tmp_path / 'opt_profile_a.json')
    p = imp.import_file(tmp_path / 'Default.json', into=p)
    assert p['optimizers'][0]['id'] == 'opt_profile_a'
    assert p['optimizers'][0]['motors'][0]['role'] == 'Jet_X'
    assert p['optimizers'][0]['config']['run_mode'] == 'Nelder-Mead (simplex)'
    assert p['xy_groups'][0]['actuator_y'] == 'Jet_Y'
    assert p['alignment']['axes'][0]['h']['actuator'] == 'Pico_OC_H'
    assert rm.module_for('Jet_Y') == 'Jet_Y'


# --------------------------------------------------------------------------- recording
def _wait_writer(rec, n, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if rec.writer is None or rec.writer.n_snapshots >= n:
            return
        time.sleep(0.05)


def test_snapshot_contains_all_roles_and_channels():
    rig = Rig()
    rig.frame(s=1.0, extra_channel=7.0)
    values, meta = build_snapshot(rig.core)
    assert values['act/M1/pos'] == 0.0
    assert values['sig/S'] == 1.0
    assert values['raw/extra_channel'] == 7.0
    assert 'mod/unmapped/pos' in values
    assert meta['global'] == 'IDLE'


def test_recorder_periodic_burst_and_loader(tmp_path):
    rig = Rig()
    rig.frame(s=1.0)
    rec = Recorder(rig.core, tmp_path, {'period_s': 1.0, 'pre_buffer_s': 2.0, 'pre_rate_hz': 2.0,
                                        'burst_duration_s': 1.0, 'burst_interval_s': 0.25,
                                        'burst_events': ['shake', 'ml_off']},
                   metadata_provider=lambda: {'machine': 'test', 'preset_name': 'p', 'roles': {'a': 1}})
    rec.start()
    for _ in range(40):                                  # 4 s
        rig.clock.advance(0.1)
        rig.frame(s=1.0)
        rec.tick()
    rig.core.shake()                                     # burst with the 2 s pre-buffer
    for _ in range(20):
        rig.clock.advance(0.1)
        rig.frame(s=2.0)
        rec.tick()
    rig.core.set_ml_state('ML_OFF')
    rig.core.add_note('test note')
    path = rec.current_path
    rec.stop()
    assert path is not None and path.exists()
    s = load_session(path)
    assert s.attrs['machine'] == 'test' and s.attrs['roles'] == {'a': 1}
    assert (s.kind == KIND_PERIODIC).sum() >= 4
    shake_id = next(e['id'] for e in s.events if e['kind'] == 'shake')
    burst = s.burst(shake_id)
    assert (burst['kind'] == KIND_PRE).sum() == 4                     # 2 s at 2 Hz before the event
    assert (burst['kind'] == KIND_BURST).sum() >= 4
    assert any(e['kind'] == 'note' and e['message'] == 'test note' for e in s.events)
    assert any(e['kind'] == 'ml' for e in s.events)
    assert 'sig/S' in s.values and len(s.values['sig/S']) == len(s.t)


def test_writer_rotation(tmp_path):
    from pymodaq_plugins_lasercontrol.recording import H5SessionWriter
    w = H5SessionWriter(tmp_path, 'rot', {'machine': 'x'}, max_mb=0.000001, flush_s=0.05)
    w.start()
    for i in range(3):
        w.write_snapshot(time.time(), 0, -1, {'a': float(i)}, {})
        time.sleep(0.4)
    w.close()
    assert len(w.paths) >= 2
    assert all(p.exists() for p in w.paths)


def test_event_trigger_files(tmp_path):
    rig = Rig()
    trig = EventTriggerWriter(rig.core.events, tmp_path / 'share', ['shake', 'ml_off'])
    rig.core.shake()
    rig.core.set_ml_state('ML_ON')
    rig.core.set_ml_state('UNSTABLE')
    files = sorted((tmp_path / 'share').glob('event_*.json'))
    assert len(files) == 2
    assert json.loads(files[0].read_text())['kind'] in ('shake', 'ml')
    trig.close()


def test_simulator_roles_file(tmp_path, monkeypatch):
    """The office tool writes the simulator roles with the lab role names; an existing roles file is
    completed, never modified."""
    pytest.importorskip('pymodaq')
    import pymodaq_plugins_lasercontrol.ui.app as app_mod
    from pymodaq_plugins_lasercontrol.core.roles import RoleMap
    from pymodaq_plugins_lasercontrol.tools import sim_preset
    orig = app_mod.resolve_paths
    monkeypatch.setattr(app_mod, 'resolve_paths', lambda config, machine, override=None: orig(config, machine, tmp_path))
    path = sim_preset.write_sim_roles()
    assert path is not None and path.exists()
    rm = RoleMap.load(path)
    assert rm.module_for('OC_H') == 'Sim OC_H' and rm.actuators['OC_H'].units == 'µm'
    assert rm.channel_for('Laser_power') == 'SimB/Laser/power'
    assert rm.module_for('Steer_H') == 'Sim STEER_H' and rm.signals['PD_current'].units == 'mA'
    assert rm.heartbeats['SimB'] == 'SimB/Stats/frame_counter'
    assert sim_preset.write_sim_roles() is None            # nothing missing: file untouched
    rm.set_actuator('OC_H', 'Mon moteur', 'pas')
    rm.remove('PD_X')
    rm.save(path)
    assert sim_preset.write_sim_roles() == path
    rm = RoleMap.load(path)
    assert rm.module_for('OC_H') == 'Mon moteur'             # existing role kept as is
    assert rm.module_for('PD_X') == 'Sim PD_X'               # missing role added
    lab = RoleMap.load(Path(__file__).resolve().parents[1] / 'docs' / 'exemples' / 'roles_lab.toml')
    assert set(lab.actuators) == set(sim_preset.SIM_ACTUATOR_ROLES)   # same role names as the lab
    assert set(lab.signals) == set(sim_preset.SIM_SIGNAL_ROLES)


def test_simulator_demo_preset(tmp_path, monkeypatch):
    """« Démo simulateur » is consistent with the simulator roles and modules: every automation can start."""
    pytest.importorskip('pymodaq')
    import pymodaq_plugins_lasercontrol.ui.app as app_mod
    from pymodaq_plugins_lasercontrol.core.roles import RoleMap
    from pymodaq_plugins_lasercontrol.presets import validate_preset, PresetStore
    from pymodaq_plugins_lasercontrol.tools import sim_preset
    orig = app_mod.resolve_paths
    monkeypatch.setattr(app_mod, 'resolve_paths', lambda config, machine, override=None: orig(config, machine, tmp_path))
    rm = RoleMap.load(sim_preset.write_sim_roles())
    p = sim_preset.demo_preset()
    modules = [m for m, _ in sim_preset.DEFAULT_MOVES]
    rep = validate_preset(p, rm, modules)
    assert rep.ok, rep.text()
    assert {a for a in rep.automations} == {'PID:Beam_H', 'PID:Beam_V', 'PID:Cam1_X_OC', 'PID:Cam1_Y_OC',
                                            'OPT:Optim_PD', 'OPT:Suivi_PD', 'OPT:Optim_OC'}
    assert all(rm.module_for(r) in modules for r in rm.actuators)
    path = sim_preset.write_demo_preset()
    assert path is not None and sim_preset.write_demo_preset() is None
    assert PresetStore(tmp_path / 'presets').load(sim_preset.DEMO_PRESET)['optimizers'][1]['button_action'] == 'periodic'


def test_schema_v1_single_optimizer_is_migrated():
    from pymodaq_plugins_lasercontrol.presets import normalize
    v1 = {'schema_version': 1, 'name': 'old', 'pids': [{'id': 'H', 'signal': 'Cam1_x'}],
          'optimizer': {'config': {'run_mode': 'Nelder-Mead (simplex)'},
                        'motors': [{'role': 'Jet_X', 'max_step': 1, 'probe_step': 0.1}],
                        'signals': [{'role': 'XUV_flux', 'mode': 'Maximize', 'weight': 1}]}}
    p = normalize(v1)
    assert p['schema_version'] == 2 and 'optimizer' not in p
    assert p['optimizers'][0]['id'] == 'Optimiseur' and p['optimizers'][0]['show_in_alignment'] is False
    assert p['optimizers'][0]['config']['run_mode'] == 'Nelder-Mead (simplex)'
    assert p['pids'][0]['show_in_alignment'] is False
    empty = normalize({'schema_version': 1, 'optimizer': {'config': {}, 'motors': [], 'signals': []}})
    assert empty['optimizers'] == []
