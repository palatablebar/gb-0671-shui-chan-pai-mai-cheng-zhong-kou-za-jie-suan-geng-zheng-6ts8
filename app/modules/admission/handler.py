import json
from copy import deepcopy

from fastapi import APIRouter, Body, Request

from app import store
from app.admission_ledger import evaluate_live
from app.config import ROOT
from app.errors import BusinessError
from app.modules.admission.rules import POLICIES, check_switch
from app.web import render

router = APIRouter()


def _db_path():
    from app.main import DB_PATH
    return DB_PATH


def _base_config():
    return json.loads((ROOT / 'sample_data' / 'admission.json').read_text(encoding='utf-8'))


def _with_policy(config, policy):
    if policy not in POLICIES:
        raise BusinessError('BAD_POLICY', f'准入策略无效：{policy}')
    merged = deepcopy(config)
    merged['policy'] = policy
    return merged


@router.get('/admission')
def page(request: Request):
    return render(request, 'admission')


@router.post('/api/admission')
def calculate(payload: dict = Body(default={})):
    config = deepcopy(payload.get('admission') or _base_config())
    if payload.get('policy'):
        config = _with_policy(config, payload['policy'])
    request = {k: v for k, v in payload.items() if k in {'dataset', 'csv_text', 'overrides'}}
    request.setdefault('overrides', {})
    request['overrides'] = {**request['overrides'], 'admission': config}
    if payload.get('simulate'):
        # 推演模式：只按源证据重算，不套用已开拍台账，用于决策前比较两种方案。
        from app.engine import evaluate
        result = evaluate(request, until=4, ledgers={})
        result['admission']['simulated'] = True
        return result
    result = evaluate_live(request, until=4)
    return result


@router.post('/api/admission/switch')
def switch(payload: dict = Body(...)):
    run_id = payload.get('run_id')
    policy = payload.get('policy')
    if not isinstance(run_id, str) or not run_id:
        raise BusinessError('BAD_RUN_ID', '须指定批次')
    previous = store.get(_db_path(), run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING', '旧批缺少源快照，请重新导入')
    if previous.get('admission', {}).get('policy') == policy:
        return previous
    config = _with_policy(previous['input_snapshot']['context'].get('admission') or _base_config(), policy)
    candidate = evaluate_live({'overrides': {'admission': config}}, snapshot=previous['input_snapshot'])
    check_switch(previous.get('admission'), candidate.get('admission'))
    candidate['switched_from'] = run_id
    candidate['admission']['switched_from'] = run_id
    candidate['admission']['previous_board'] = [r['ticket'] for r in previous.get('admission', {}).get('board', [])]
    return store.save(_db_path(), candidate)
