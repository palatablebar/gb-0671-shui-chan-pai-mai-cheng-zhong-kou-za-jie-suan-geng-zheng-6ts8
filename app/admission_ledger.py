"""已开拍冻结台账。

签发当天准入结果时，已开拍场次的准入结论（含称重依据、重量与计价）整行写入台账。
此后同批重跑、迟到校准记录或迟到复秤到达，重算都按台账原样恢复该场：
不替换依据、不改场次、不重复上板；未确认货包也不会借重跑混进可竞价范围。

台账按「交易日 + 源文件摘要」隔离，不同交易日或不同源文件不互相串用。
台账不进入批次摘要：批号仍由「源字节 + 参数 + 规则版本」决定；台账是运营事实，
不是输入。台账只增不改；原始称重证据仍完整保存在批次结果的证据账里。
"""
import json
from datetime import datetime, timezone

from app import store

DEFAULT_SCOPE = 'default'


def _ensure_columns(con):
    columns = {row['name'] for row in con.execute('PRAGMA table_info(admission_ledger)')}
    if 'row_json' not in columns:
        con.execute("ALTER TABLE admission_ledger ADD COLUMN row_json TEXT NOT NULL DEFAULT ''")


def scope_of(result):
    return (result.get('admission') or {}).get('scope') or (result.get('admission_scope')
                                                           or DEFAULT_SCOPE)


def load_all(path):
    """返回 {scope: {session_id: {ticket: row_json}}}。"""
    with store.connect(path) as con:
        _ensure_columns(con)
        rows = con.execute(
            'SELECT scope,session_id,ticket,basis,evidence_id,row_json FROM admission_ledger'
            ' ORDER BY scope,session_id,ticket').fetchall()
    ledger = {}
    for row in rows:
        payload = json.loads(row['row_json']) if row['row_json'] else {
            'ticket': row['ticket'], 'basis_evidence': row['basis'],
            'evidence_id': row['evidence_id'], 'session_id': row['session_id'],
            'status': '已开拍',
        }
        ledger.setdefault(row['scope'], {}).setdefault(row['session_id'], {})[row['ticket']] = payload
    return ledger


def load(path, scope=DEFAULT_SCOPE):
    return load_all(path).get(scope, {})


def record(path, result):
    """把已开拍场次的整行准入结论写入台账；只增不改。"""
    admission = result.get('admission') or {}
    scope = scope_of(result)
    opened = {s['session_id'] for s in admission.get('sessions') or [] if s.get('status') == '已开拍'}
    entries = [
        (scope, row['session_id'], row['ticket'], row['basis_evidence'], row['evidence_id'],
         json.dumps(row, ensure_ascii=False),
         result.get('manifest', {}).get('run_id', ''), datetime.now(timezone.utc).isoformat())
        for row in admission.get('board', [])
        if row['session_id'] in opened and row.get('evidence_id')
    ]
    if not entries:
        return 0
    with store.connect(path) as con:
        _ensure_columns(con)
        con.executemany(
            'INSERT OR IGNORE INTO admission_ledger'
            '(scope,session_id,ticket,basis,evidence_id,row_json,first_run_id,recorded_at)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            entries,
        )
    return len(entries)


def evaluate_live(payload=None, until=None, snapshot=None):
    """带冻结台账重算：所有对外结果与保存结果都走这里。"""
    from app.engine import evaluate
    from app.main import DB_PATH
    return evaluate(payload, until=until, snapshot=snapshot, ledgers=load_all(DB_PATH))
