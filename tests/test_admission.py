import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import admission_ledger, main, store
from app.config import ROOT
from app.engine import evaluate
from app.errors import BusinessError
from app.modules.admission.rules import check_switch

SCOPE = 'donggang-20261118'


def config():
    return json.loads((ROOT / 'sample_data' / 'admission.json').read_text(encoding='utf-8'))


def with_config(change):
    cfg = config()
    change(cfg)
    return evaluate({'overrides': {'admission': cfg}})


@pytest.fixture
def saved(tmp_path):
    path = str(tmp_path / 'ledger.sqlite')
    run = evaluate()
    store.save(path, run)
    return path, run


def test_first_session_keeps_open_time(sample):
    first = sample['admission']['sessions'][0]
    assert first['opened_at'] == '2026-11-18T05:30:00+08:00'
    assert len(first['admitted']) == 17
    assert first['status'] == '已开拍'


def test_unaffected_package_on_board(sample):
    row = next(r for r in sample['admission']['board'] if r['ticket'] == '00003')
    assert row['status'] == '已开拍'
    assert row['basis_evidence'] == '原始称重'
    assert row['evidence_id'] == 'W-00003-ORIG'


def test_affected_package_uses_reweigh(sample):
    row = next(r for r in sample['admission']['board'] if r['ticket'] == '00002')
    assert row['basis_evidence'] == '复秤'
    assert row['evidence_id'] == 'W-00002-RW1'
    assert row['net_kg'] == '92'


def test_affected_package_held_until_reweigh(sample):
    row = next(r for r in sample['admission']['rows'] if r['ticket'] == '00008')
    assert row['session_id'] == 'OPEN-0620'
    assert row['basis_evidence'] == '复秤'
    assert 'AFFECTED_SCALE' not in row['blockers']


def test_unconfirmed_never_boarded(sample):
    board = {r['ticket'] for r in sample['admission']['board']}
    held = {r['ticket'] for r in sample['admission']['held']}
    assert board.isdisjoint(held)
    for ticket in ('00901', '00902', '00903', '00904', '00905'):
        assert ticket not in board
        assert ticket in held
    assert len([r['ticket'] for r in sample['admission']['board']]) == len(board)


def test_every_boarded_package_has_evidence(sample):
    for row in sample['admission']['board']:
        assert row['evidence_id'].startswith('W-')
        assert row['net_kg'] not in ('', None)
        assert row['amount'] not in ('', None)


def test_unadmitted_excluded_from_auction(sample):
    tickets = {row['ticket'] for row in sample['settlements']}
    assert tickets.isdisjoint({'00901', '00902', '00903', '00904', '00905'})
    assert len(sample['unadmitted']) == 5
    assert sample['balance']['amount'] == '23708.91'


def test_original_evidence_retained(sample):
    evidence = {row['evidence_id']: row for row in sample['admission']['evidence']}
    assert evidence['W-00002-ORIG']['retained'] == '是'
    assert evidence['W-00002-ORIG']['usage'] == '留档（已被复秤替代）'
    assert all(row['retained'] == '是' for row in evidence.values())
    assert {'W-00001-ORIG', 'W-00002-RW1', 'W-00001-RW1'} <= set(evidence)


def test_whole_policy_waits_for_all(sample):
    whole = with_config(lambda cfg: cfg.update(policy='whole'))['admission']
    assert whole['sessions'][0]['status'] == '未开拍'
    assert len(whole['sessions'][0]['admitted']) == 0
    assert len(whole['sessions'][1]['admitted']) == 20
    assert len(whole['board']) == 20


def test_comparison_uses_same_evidence(sample):
    rows = {row['policy']: row for row in sample['admission']['comparison']}
    assert rows['分场准入']['首场'] == '17 包'
    assert rows['整场等待']['首场'] == '0 包'
    assert rows['分场准入']['当日'] == rows['整场等待']['当日'] == '20 包'


def test_late_calibration_cannot_swap_basis(tmp_path):
    path = str(tmp_path / 'late.sqlite')
    first = evaluate()
    store.save(path, first)
    ledgers = admission_ledger.load_all(path)
    cfg = config()
    cfg['calibrations'].append({
        'record_id': 'CAL-S1-0610', 'scale_id': 'S1', 'kind': 'anomaly',
        'from': '2026-11-18T04:30:00+08:00', 'to': '2026-11-18T04:40:00+08:00',
        'recorded_at': '2026-11-18T05:40:00+08:00', 'note': '迟到',
    })
    frozen = evaluate({'overrides': {'admission': cfg}}, ledgers=ledgers)['admission']
    fresh = evaluate({'overrides': {'admission': cfg}}, ledgers={})['admission']
    row = next(r for r in frozen['board'] if r['ticket'] == '00003')
    assert row['evidence_id'] == 'W-00003-ORIG'
    assert {r['ticket'] for r in frozen['board']} == {r['ticket'] for r in first['admission']['board']}
    assert len(frozen['board']) == len(fresh['board'])


def test_missing_source_evidence_keeps_frozen_row(tmp_path):
    path = str(tmp_path / 'missing.sqlite')
    first = evaluate()
    store.save(path, first)
    ledgers = admission_ledger.load_all(path)
    cfg = config()
    cfg['weighings'] = [w for w in cfg['weighings'] if w['evidence_id'] != 'W-00003-ORIG']
    frozen = evaluate({'overrides': {'admission': cfg}}, ledgers=ledgers)['admission']
    row = next(r for r in frozen['board'] if r['ticket'] == '00003')
    assert row['evidence_id'] == 'W-00003-ORIG'
    assert row['amount'] == next(r['amount'] for r in first['admission']['board'] if r['ticket'] == '00003')


def test_rerun_keeps_board_and_run_id(sample):
    again = evaluate(snapshot=sample['input_snapshot'])
    assert again['manifest']['run_id'] == sample['manifest']['run_id']
    assert [r['ticket'] for r in again['admission']['board']] == [r['ticket'] for r in sample['admission']['board']]
    assert [r['evidence_id'] for r in again['admission']['board']] == [r['evidence_id'] for r in sample['admission']['board']]


def test_switch_guard_rejects_dropping_opened(sample):
    candidate = deepcopy(sample['admission'])
    candidate['board'] = [row for row in candidate['board'] if row['ticket'] != '00003']
    with pytest.raises(BusinessError) as error:
        check_switch(sample['admission'], candidate)
    assert error.value.code == 'FROZEN_PACKAGE_DROP'
    assert error.value.status == 409


def test_switch_guard_rejects_moving_opened(sample):
    whole = with_config(lambda cfg: cfg.update(policy='whole'))['admission']
    with pytest.raises(BusinessError) as error:
        check_switch(sample['admission'], whole)
    assert error.value.code == 'FROZEN_PACKAGE_CHANGED'
    assert error.value.status == 409


def test_switch_guard_rejects_duplicate(sample):
    candidate = deepcopy(sample['admission'])
    candidate['board'].append(dict(candidate['board'][0]))
    with pytest.raises(BusinessError) as error:
        check_switch(sample['admission'], candidate)
    assert error.value.code == 'DUPLICATE_PACKAGE'


def test_bad_policy_refused(sample):
    with pytest.raises(BusinessError) as error:
        evaluate({'overrides': {'admission': {**config(), 'policy': 'rush'}}})
    assert error.value.code == 'BAD_POLICY'


def test_admission_page_and_api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    client = TestClient(main.app)
    with client:
        page = client.get('/admission')
        assert page.status_code == 200
        assert '暂缓清单' in page.text and '可竞价范围' in page.text
        run = client.post('/api/admission', json={}).json()
        assert run['admission']['metrics']['已开拍'] == 20
        # 已开拍货包写入台账后，即使改用整场策略也不得撤板。
        payload = client.post('/api/admission', json={'policy': 'whole'}).json()
        assert payload['admission']['metrics']['已开拍'] == 20
        assert client.post('/api/admission', json={'policy': 'nope'}).status_code == 422


def test_whole_policy_api_on_fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'fresh.sqlite'))
    client = TestClient(main.app)
    with client:
        # 推演模式不套用已开拍台账，才能看到「整场等待」在决策前的真实代价。
        payload = client.post('/api/admission', json={'policy': 'whole', 'simulate': True}).json()
        assert payload['admission']['metrics']['首场可开拍'] == 0
        assert payload['admission']['sessions'][0]['status'] == '未开拍'
        assert payload['admission']['simulated'] is True


def test_switch_api_keeps_original_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'switch.sqlite'))
    client = TestClient(main.app)
    with client:
        run = client.post('/api/runs', json={}).json()
        run_id = run['manifest']['run_id']
        before = client.get('/api/runs/' + run_id).json()
        bad = client.post('/api/admission/switch', json={'run_id': run_id, 'policy': 'nope'})
        assert bad.status_code == 422
        after = client.get('/api/runs/' + run_id).json()
        assert after['admission']['board'] == before['admission']['board']
        assert after['revision'] == before['revision']


def test_switch_api_same_board(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'switch2.sqlite'))
    client = TestClient(main.app)
    with client:
        run = client.post('/api/runs', json={}).json()
        run_id = run['manifest']['run_id']
        switched = client.post('/api/admission/switch', json={'run_id': run_id, 'policy': 'whole'}).json()
        assert [r['ticket'] for r in switched['admission']['board']] == [r['ticket'] for r in run['admission']['board']]
        assert switched['admission']['switched_from'] == run_id


def test_ledger_scoped_by_trading_day(saved):
    path, run = saved
    scope = run['admission']['scope']
    assert scope.startswith(SCOPE + ':')
    ledger = admission_ledger.load(path, scope)
    assert len(ledger['OPEN-0530']) == 17
    assert len(ledger['OPEN-0620']) == 3
    assert admission_ledger.load(path, 'another-day') == {}


def test_ledger_does_not_leak_across_sources(saved):
    path, run = saved
    # 同一交易日、另一份源文件（核验清批）不能沿用脏批的已开拍结论。
    clean = evaluate({'dataset': 'clean'})
    assert clean['admission']['scope'] != run['admission']['scope']
    ledgers = admission_ledger.load_all(path)
    assert clean['admission']['scope'] not in ledgers


def test_frozen_to_later_session_not_opened_early(sample):
    scope = sample['admission']['scope']
    row = dict(next(r for r in sample['admission']['board'] if r['ticket'] == '00002'),
               session_id='OPEN-0620', session_label='补拍',
               opened_at='2026-11-18T06:20:00+08:00')
    replayed = evaluate(ledgers={scope: {'OPEN-0620': {'00002': row}}})['admission']
    first = next(s for s in replayed['sessions'] if s['session_id'] == 'OPEN-0530')
    assert '00002' not in first['admitted']
    moved = next(r for r in replayed['rows'] if r['ticket'] == '00002')
    assert moved['session_id'] == 'OPEN-0620'
    assert len(replayed['board']) == len({r['ticket'] for r in replayed['board']})


def test_invalid_reweigh_kept_off_board():
    cfg = config()
    for weighing in cfg['weighings']:
        if weighing['evidence_id'] == 'W-00002-RW1':
            weighing['gross_kg'] = '5'
    admission = evaluate({'overrides': {'admission': cfg}})['admission']
    row = next(r for r in admission['rows'] if r['ticket'] == '00002')
    assert row['status'] != '已开拍'
    assert 'INVALID_REWEIGH' in row['blockers']
    assert '00002' not in {r['ticket'] for r in admission['board']}
    assert '00002' in {r['ticket'] for r in admission['held']}


def test_decision_time_blocks_future_session():
    cfg = config()
    cfg['as_of'] = '2026-11-18T05:45:00+08:00'
    admission = evaluate({'overrides': {'admission': cfg}})['admission']
    first, second = admission['sessions']
    assert first['status'] == '已开拍' and len(first['admitted']) == 17
    assert second['status'] == '待开拍' and second['admitted'] == []
    # 决策时点之后才到齐的货包仍留在暂缓清单，不会提前上板。
    assert '00008' in {r['ticket'] for r in admission['held']}
    assert '00008' not in {r['ticket'] for r in admission['board']}


def test_as_of_before_first_session_boards_nothing():
    cfg = config()
    cfg['as_of'] = '2026-11-18T05:20:00+08:00'
    admission = evaluate({'overrides': {'admission': cfg}})['admission']
    assert admission['board'] == []
    assert all(s['status'] == '待开拍' for s in admission['sessions'])
