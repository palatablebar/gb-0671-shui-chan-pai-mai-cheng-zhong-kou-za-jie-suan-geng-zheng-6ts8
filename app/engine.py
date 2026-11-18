import hashlib
from importlib import import_module
from pathlib import Path
from copy import deepcopy
import json, time, base64
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest

def ledger_scope(context, raw):
    """冻结台账作用域 = 交易日 + 源文件身份。

    交易日由 `admission.scope` 给出；再拼源文件摘要，避免同一交易日里不同来源文件
    （例如现场脏批与核验清批）的票号互相套用已开拍结论。策略与规则版本不参与，
    因此切换方案或升级规则都不会丢掉已开拍冻结。
    """
    day = (context.get('admission') or {}).get('scope', 'default')
    return f"{day}:{hashlib.sha256(raw).hexdigest()[:12]}"

def evaluate(payload=None, until=None, snapshot=None, ledgers=None):
    payload=payload or {}
    dataset=payload.get('dataset','dirty')
    if dataset not in {'dirty','clean','legacy'}: raise BusinessError('BAD_DATASET','样例名称无效')
    if not isinstance(payload.get('overrides',{}),dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    raw=(base64.b64decode(snapshot['bytes_b64'],validate=True) if snapshot is not None else (str(payload['csv_text']).encode() if 'csv_text' in payload else (ROOT/'sample_data'/f'{dataset}.csv').read_bytes()))
    if len(raw)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    context=deepcopy(snapshot['context']) if snapshot is not None else json.loads((ROOT/'sample_data/context.json').read_text())
    if 'admission' not in context:
        context['admission']=json.loads((ROOT/'sample_data/admission.json').read_text(encoding='utf-8'))
    context.update(deepcopy(payload.get('overrides',{})))
    start=time.perf_counter()
    state={'raw_bytes':raw,'context':context,'records':[],'quarantine':[], 'counts':{},'views':{},'source':dataset}
    scope=ledger_scope(context,raw)
    state['admission_scope']=scope
    state['admission_ledger']=deepcopy((ledgers or {}).get(scope,{}))
    for index,key in enumerate(MODULES,1):
        if until is not None and index>until: break
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot']={'bytes_b64':base64.b64encode(raw).decode(),'context':deepcopy(context)}
    state.pop('raw_bytes',None)
    state['manifest']=make_manifest(digest(raw,context),dataset,time.perf_counter()-start)
    return state
