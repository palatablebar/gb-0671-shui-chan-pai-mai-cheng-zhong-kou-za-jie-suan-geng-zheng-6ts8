"""开拍准入（M4）

把「校准异常记录」与「称重证据」按到达时点对齐，逐场判定货包能否上板：

* 每个场次只认开拍时点**之前已到达**的校准记录与称重证据，场次成员因此天然冻结；
  迟到记录不会回改已开拍场次，已开拍货包不替换、不重复。
* 原始称重证据一律留档：复秤只追加依据，不删除、不覆盖原记录。
* 未能确认的货包只进暂缓清单，绝不进入可竞价范围。
* 切换准入方案前先校验，失败抛错且不改动已保存的准入结果。
"""
from collections import defaultdict
from decimal import Decimal

from app.coerce import instant
from app.errors import BusinessError
from app.modules.auction.rules import settle

POLICIES = {'staged', 'whole'}
POLICY_LABELS = {'staged': '分场准入（未受影响先开拍）', 'whole': '整场等待（全部确认后开拍）'}
BLOCKER_LABELS = {
    'PAIRING_QUARANTINED': '毛皮配对隔离',
    'AFFECTED_SCALE': '原称重落在异常时段',
    'NEEDS_REWEIGH': '缺有效复秤',
    'INVALID_REWEIGH': '复秤重量无效',
    'NO_WEIGHING_EVIDENCE': '称重证据未到',
    'WHOLE_WAIT': '整场等待',
    'FROZEN_WAIT': '已冻结在后续场次',
}
STATUS_BY_BLOCKER = (
    ('PAIRING_QUARANTINED', '配对隔离'),
    ('AFFECTED_SCALE', '待复秤'),
    ('NEEDS_REWEIGH', '待复秤'),
    ('INVALID_REWEIGH', '待复秤'),
    ('NO_WEIGHING_EVIDENCE', '待称重证据'),
    ('FROZEN_WAIT', '后续场次已开拍'),
    ('WHOLE_WAIT', '待开拍'),
)


def _label(codes):
    return '、'.join(BLOCKER_LABELS.get(c, c) for c in codes)


def _status(codes):
    for code, text in STATUS_BY_BLOCKER:
        if code in codes:
            return text
    return '暂缓'


def _load(cfg):
    calibrations = [
        {'record_id': c['record_id'], 'scale_id': c['scale_id'],
         'kind': c.get('kind', 'anomaly'), 'start': instant(c['from']), 'end': instant(c['to']),
         'recorded_at': instant(c['recorded_at']), 'recorded_text': c['recorded_at'],
         'note': c.get('note', '')}
        for c in cfg.get('calibrations', [])
    ]
    weighings = [
        {'evidence_id': w['evidence_id'], 'ticket': w['ticket'], 'kind': w['kind'],
         'scale_id': w['scale_id'], 'weighed_at': instant(w['weighed_at']),
         'recorded_at': instant(w['recorded_at']),
         'weighed_text': w['weighed_at'], 'recorded_text': w['recorded_at'],
         'gross_kg': w.get('gross_kg'), 'tare_kg': w.get('tare_kg')}
        for w in cfg.get('weighings', [])
    ]
    sessions = sorted(
        ({'session_id': s['session_id'], 'label': s.get('label', s['session_id']),
          'opened_at': instant(s['opened_at']), 'opened_text': s['opened_at']}
         for s in cfg.get('sessions', [])),
        key=lambda s: s['opened_at'],
    )
    packages = [{'ticket': p['ticket'], 'lot_id': p['lot_id']} for p in cfg.get('packages', [])]
    lots = [{'lot_id': l['lot_id'], 'lot_name': l.get('lot_name', l['lot_id'])}
            for l in cfg.get('lots', [])]
    lot_names = {l['lot_id']: l['lot_name'] for l in lots}
    for pkg in packages:
        pkg['lot_name'] = lot_names.get(pkg['lot_id'], pkg['lot_id'])
    return calibrations, weighings, sessions, packages, lots


def _anomalies(calibrations, known_by):
    return [c for c in calibrations
            if c['kind'] == 'anomaly' and c['recorded_at'] <= known_by]


def _covered(anomaly, weighing):
    return (anomaly['scale_id'] == weighing['scale_id']
            and anomaly['start'] <= weighing['weighed_at'] <= anomaly['end'])


def _originals(weighings, ticket):
    rows = [w for w in weighings if w['ticket'] == ticket and w['kind'] == 'original']
    return sorted(rows, key=lambda w: (w['recorded_at'], w['weighed_at'], w['evidence_id']))


def _reweighs(weighings, ticket):
    rows = [w for w in weighings if w['ticket'] == ticket and w['kind'] == 'reweigh']
    return sorted(rows, key=lambda w: (w['weighed_at'], w['recorded_at'], w['evidence_id']))


def _assess(ticket, cutoff, calibrations, weighings, quarantined):
    """判定货包在 cutoff 时点的可用依据。返回 (basis, evidence, blockers, ready_at)。"""
    originals = [w for w in _originals(weighings, ticket) if w['recorded_at'] <= cutoff]
    known = _anomalies(calibrations, cutoff)
    affected = next((w for w in originals if any(_covered(a, w) for a in known)), None)
    if originals and affected is None:
        return 'original', originals[-1], [], ''
    if affected is None:
        if quarantined:
            return None, None, ['PAIRING_QUARANTINED'], ''
        return None, None, ['NO_WEIGHING_EVIDENCE'], ''
    reweighs = [w for w in _reweighs(weighings, ticket)
                if w['recorded_at'] <= cutoff and w['weighed_at'] <= cutoff
                and not any(_covered(a, w) for a in known)]
    if reweighs:
        return 'reweigh', reweighs[-1], [], ''
    later = [w for w in _reweighs(weighings, ticket)
             if not any(_covered(a, w) for a in known)]
    ready_at = later[0]['recorded_text'] if later else ''
    return None, None, ['AFFECTED_SCALE', 'NEEDS_REWEIGH'], ready_at


def _row(pkg, pair, version):
    return {
        'ticket': pkg['ticket'], 'lot_id': pkg['lot_id'], 'lot_name': pkg['lot_name'],
        'version': version, 'status': '暂缓', 'session_id': '', 'session_label': '', 'opened_at': '',
        'basis_evidence': '', 'evidence_id': '', 'scale_id': '', 'weighed_at': '',
        'gross_kg': '', 'tare_kg': '', 'net_kg': '', 'billable_kg': '', 'amount': '',
        'price': pair['price'] if pair else '', 'rate': pair['rate'] if pair else '',
        'flags': '', 'blockers': [], 'blockers_text': '', 'detail': '',
        'held_in': [], 'ready_at': '',
    }


def _decide(state, cfg):
    calibrations, weighings, sessions, packages, lots = _load(cfg)
    policy = cfg.get('policy', 'staged')
    if policy not in POLICIES:
        raise BusinessError('BAD_POLICY', f'准入策略无效：{policy}')

    pairs = {}
    for pair in state.get('pairs', []):
        current = pairs.get(pair['ticket'])
        if current is None or int(pair['version']) > int(current['version']):
            pairs[pair['ticket']] = pair
    quarantined = defaultdict(list)
    for entry in state.get('quarantine', []):
        ticket = (entry.get('raw') or {}).get('ticket', '')
        if ticket:
            quarantined[ticket].append(entry['code'])

    # 清单里完全不在本批源文件中的货包不属于本场判定对象；否则会把「别的批才有」
    # 的票号误报成配对隔离。真正缺点在于本批有原始行却没称重证据的情形，仍会列入暂缓。
    present = set(pairs) | set(quarantined)
    outside = [p['ticket'] for p in packages if p['ticket'] not in present]
    packages = [p for p in packages if p['ticket'] in present]

    ledger = state.get('admission_ledger') or {}
    # 某票已冻结在别的场次：在更早的场次里必须继续等待，不能提前改到本场开拍。
    frozen_session = {ticket: session_id
                      for session_id, session_rows in ledger.items()
                      for ticket in session_rows}
    # 决策时点：只评估到此刻已开拍的场次；之后的场次仅作为「待开拍」出现在时间线上。
    as_of = instant(cfg['as_of']) if cfg.get('as_of') else None
    rows, admitted = {}, {}
    session_results = []
    for session in sessions:
        cutoff = session['opened_at']
        frozen = ledger.get(session['session_id'], {})
        if as_of is not None and cutoff > as_of and not frozen:
            # 决策时点之后的场次尚未开拍：只出现在时间线上。
            # 台账里已有冻结记录的场次视为实际已开拍，不受 as_of 回拨影响。
            session_results.append({
                'session_id': session['session_id'], 'label': session['label'],
                'opened_at': session['opened_text'], 'status': '待开拍',
                'admitted': [], 'held': [], 'scheduled': True,
            })
            continue
        pending = [p for p in packages if p['ticket'] not in admitted]
        decisions, frozen_rows = {}, {}
        for pkg in pending:
            ticket = pkg['ticket']
            if ticket in frozen:
                # 台账优先：整行原样恢复开拍结论，不重新选依据。
                frozen_rows[ticket] = frozen[ticket]
                decisions[ticket] = ('reweigh' if frozen[ticket]['basis_evidence'] == '复秤'
                                     else 'original', None, [], '')
                continue
            if frozen_session.get(ticket, session['session_id']) != session['session_id']:
                decisions[ticket] = (None, None, ['FROZEN_WAIT'], '')
                continue
            if ticket not in pairs:
                decisions[ticket] = (None, None, ['PAIRING_QUARANTINED'], '')
            else:
                decisions[ticket] = _assess(ticket, cutoff, calibrations, weighings,
                                            quarantined.get(ticket))
        restore = [p for p in pending if p['ticket'] in frozen_rows]
        fresh = [p for p in pending if p['ticket'] not in frozen_rows
                 and decisions[p['ticket']][0] is not None]
        # 整场等待：等待对象是「尚有配对、可能确认」的货包；永久配对隔离的货包单独排除，
        # 也排除已冻结在后续场次的货包，否则当前场次会被历史冻结拖住。
        barrier = [p for p in pending
                   if p['ticket'] not in frozen_rows and p['ticket'] in pairs
                   and frozen_session.get(p['ticket'], session['session_id']) == session['session_id']]
        if policy == 'whole' and len(fresh) != len(barrier):
            fresh = []
        # 台账里的已开拍货包永远保留在竞价板上，任何策略或迟到记录都不能把它们撤下。
        board = restore + fresh
        board_tickets = {p['ticket'] for p in board}
        held = [p for p in pending if p['ticket'] not in board_tickets]

        for pkg in board:
            ticket = pkg['ticket']
            if ticket in frozen_rows:
                # 整行恢复：场次、依据号、重量、金额全部沿用开拍时的值。
                row = dict(frozen_rows[ticket])
                originals = [w for w in weighings if w['ticket'] == ticket]
                for weighing in originals:
                    if weighing['recorded_at'] > next(
                            s['opened_at'] for s in sessions if s['session_id'] == row['session_id']):
                        row['flags'] = '、'.join(filter(None, [row.get('flags', ''),
                                                              '开拍后证据留档未采用']))
                        break
                row['held_in'] = list(rows.get(ticket, {}).get('held_in', []))
                rows[ticket] = row
                admitted[ticket] = row
                continue
            pair = pairs[ticket]
            basis, evidence, _, _ = decisions[ticket]
            if basis == 'reweigh':
                gross, tare = evidence['gross_kg'], evidence['tare_kg']
            else:
                gross, tare = pair['gross_kg'], pair['tare_kg']
            try:
                net = Decimal(str(gross)) - Decimal(str(tare))
            except Exception:
                net = None
            if net is None or net < 0:
                # 复秤重量本身不可用：撤出本场可竞价集合，留在暂缓清单。
                decisions[ticket] = (None, None, ['INVALID_REWEIGH'], '')
                board_tickets.discard(ticket)
                held.append(pkg)
                continue
            row = _row(pkg, pair, pair['version'])
            row['held_in'] = list(rows.get(ticket, {}).get('held_in', []))
            priced = settle({'net_kg': str(net), 'rate': pair['rate'], 'price': pair['price']})
            row.update(status='已开拍', session_id=session['session_id'],
                       session_label=session['label'], opened_at=session['opened_text'],
                       basis_evidence='复秤' if basis == 'reweigh' else '原始称重',
                       evidence_id=evidence['evidence_id'], scale_id=evidence['scale_id'],
                       weighed_at=evidence['weighed_text'], gross_kg=str(gross),
                       tare_kg=str(tare), net_kg=str(net),
                       billable_kg=priced['billable_kg'], amount=priced['amount'])
            if basis == 'reweigh' and str(net) != pair['net_kg']:
                row['flags'] = '复秤净重与台账不一致'
            rows[ticket] = row
            admitted[ticket] = row

        for pkg in held:
            ticket = pkg['ticket']
            _, _, blockers, ready_at = decisions[ticket]
            if ticket in rows:
                rows[ticket]['held_in'].append(session['session_id'])
                continue
            pair = pairs.get(ticket)
            if policy == 'whole' and decisions[ticket][0] is not None:
                blockers = ['WHOLE_WAIT']
            row = _row(pkg, pair, pair['version'] if pair else '')
            row['blockers'] = list(dict.fromkeys(blockers))
            row['blockers_text'] = _label(row['blockers'])
            row['status'] = _status(row['blockers'])
            row['held_in'] = [session['session_id']]
            row['ready_at'] = ready_at
            if pair is None:
                row['detail'] = '、'.join(sorted(set(quarantined.get(ticket, [])))) or '无有效配对'
            elif 'FROZEN_WAIT' in row['blockers']:
                row['detail'] = '该货包已冻结在后续场次开拍，本场不重复上板'
            elif 'WHOLE_WAIT' in row['blockers']:
                row['detail'] = '整场策略下等待全部货包确认'
            elif blockers:
                row['detail'] = '未达开拍条件，留在暂缓清单'
            rows[ticket] = row

        session_results.append({
            'session_id': session['session_id'], 'label': session['label'],
            'opened_at': session['opened_text'],
            'status': '已开拍' if board else ('未开拍' if policy == 'whole' else '无可开拍货包'),
            'admitted': sorted(board_tickets),
            'held': sorted(p['ticket'] for p in held),
        })

    board_rows = [rows[t] for t in sorted(admitted)]
    held_rows = [rows[t] for t in sorted(rows) if rows[t]['status'] != '已开拍']

    # 迟到审计：记录在开拍之后到达，但覆盖了已开拍货包的原始称重。
    anomalies = [c for c in calibrations if c['kind'] == 'anomaly']
    audit = []
    for ticket, row in sorted(admitted.items()):
        cutoff = next(s['opened_at'] for s in sessions if s['session_id'] == row['session_id'])
        original = _originals(weighings, ticket)
        late = [a for a in anomalies
                if a['recorded_at'] > cutoff
                and any(o['scale_id'] == a['scale_id'] and _covered(a, o) for o in original)]
        if late:
            row['flags'] = '、'.join(filter(None, [row['flags'], '迟到校准记录']))
            row['detail'] = '；'.join(
                f"{a['record_id']} 于 {a['recorded_text']} 到达，覆盖原称重；"
                f"已开拍冻结，不再改换或重复"
                for a in late)
            audit.append({
                'code': 'LATE_CALIBRATION', 'session_id': row['session_id'],
                'session_label': row['session_label'], 'tickets': [ticket],
                'recorded_at': '、'.join(a['recorded_text'] for a in late),
                'detail': f"{'、'.join(a['record_id'] for a in late)} 开拍后到达；"
                          f"货包 {ticket} 保持已开拍依据 {row['evidence_id']}，不替换不重复",
            })
        late_evidence = [w for w in weighings
                         if w['ticket'] == ticket and w['kind'] == 'reweigh'
                         and w['recorded_at'] > cutoff]
        for weighing in late_evidence:
            audit.append({
                'code': 'LATE_EVIDENCE', 'session_id': row['session_id'],
                'session_label': row['session_label'], 'tickets': [ticket],
                'recorded_at': weighing['recorded_text'],
                'detail': f"{weighing['evidence_id']} 开拍后到达，仅留档，不改换已开拍货包",
            })

    # 证据账：原始证据永不出列，只标注用途。
    board_evidence = {row['evidence_id'] for row in board_rows}
    superseded = {row['ticket'] for row in board_rows if row['basis_evidence'] == '复秤'}
    admitted_tickets = set(admitted)
    evidence_rows = []
    for weighing in weighings:
        ticket = weighing['ticket']
        if weighing['evidence_id'] in board_evidence:
            usage = '已用作依据'
        elif weighing['kind'] == 'original' and ticket in superseded:
            usage = '留档（已被复秤替代）'
        elif ticket in admitted_tickets:
            usage = '留档（未采用）'
        elif ticket in quarantined:
            usage = '留档（配对隔离）'
        else:
            usage = '留档（未开拍）'
        evidence_rows.append({
            'evidence_id': weighing['evidence_id'], 'ticket': ticket,
            'kind': '复秤' if weighing['kind'] == 'reweigh' else '原始称重',
            'scale_id': weighing['scale_id'], 'weighed_at': weighing['weighed_text'],
            'recorded_at': weighing['recorded_text'], 'retained': '是', 'usage': usage,
        })

    lot_rows = []
    for lot in lots:
        members = [p['ticket'] for p in packages if p['lot_id'] == lot['lot_id']]
        state_rows = [rows[t] for t in members if t in rows]
        if not state_rows:
            continue
        admitted_rows = [r for r in state_rows if r['status'] == '已开拍']
        held = [r for r in state_rows if r['status'] != '已开拍']
        blockers = sorted({c for r in held for c in r['blockers'] if c != 'WHOLE_WAIT'})
        status = '可开拍' if not held else ('整批暂缓' if not admitted_rows else '部分暂缓')
        first = sessions[0]['session_id'] if sessions else ''
        lot_rows.append({
            'lot_id': lot['lot_id'], 'lot_name': lot['lot_name'], 'status': status,
            'packages': len(state_rows), 'admitted': len(admitted_rows), 'held': len(held),
            'first_session': sum(1 for r in admitted_rows if r['session_id'] == first),
            'tickets': '、'.join(members),
            'blockers_text': '、'.join(BLOCKER_LABELS.get(c, c) for c in blockers),
        })

    return {
        'policy': policy, 'policy_label': POLICY_LABELS[policy],
        'scope': state.get('admission_scope', ''),
        'as_of': cfg.get('as_of', ''),
        'sessions': session_results, 'board': board_rows, 'held': held_rows,
        'rows': board_rows + held_rows, 'lots': lot_rows, 'audit': audit,
        'evidence': evidence_rows,
        'metrics': {
            '货包': len(rows), '已开拍': len(board_rows), '暂缓': len(held_rows),
            '首场可开拍': len(session_results[0]['admitted']) if session_results else 0,
            '场次': len(session_results),
        },
        'counts': {'board': len(board_rows), 'held': len(held_rows),
                   'evidence': len(evidence_rows), 'packages': len(rows)},
        'outside_packages': outside,
    }


def comparison(state, cfg):
    """方案对比是反事实推演：只按同一批证据重算，不叠加已开拍台账。"""
    probe = {**state, 'admission_ledger': {}}
    staged = _decide(probe, {**cfg, 'policy': 'staged'})
    whole = _decide(probe, {**cfg, 'policy': 'whole'})
    first = staged['sessions'][0] if staged['sessions'] else None
    staged_first = len(first['admitted']) if first else 0
    whole_first = len(whole['sessions'][0]['admitted']) if whole['sessions'] else 0
    whole_arrive = next((s['opened_at'] for s in whole['sessions'] if s['admitted']),
                        '当日无法开拍')
    return [
        {'policy': '分场准入', '首场': f'{staged_first} 包',
         '当日': f"{len(staged['board'])} 包", '第一场时点': first['opened_at'] if first else '',
         '说明': '未受影响货包按点开拍，异常货包复秤后补拍'},
        {'policy': '整场等待', '首场': f'{whole_first} 包',
         '当日': f"{len(whole['board'])} 包", '第一场时点': first['opened_at'] if first else '',
         '说明': f'任何货包未确认则整场不开拍；最早开拍时点 {whole_arrive}'},
    ]


def apply(state):
    cfg = (state.get('context') or {}).get('admission')
    if not cfg:
        state['admission'] = {'policy': '', 'policy_label': '未配置', 'board': [], 'held': [],
                              'rows': [], 'lots': [], 'sessions': [], 'audit': [], 'evidence': [],
                              'metrics': {'货包': 0, '已开拍': 0, '暂缓': 0}, 'counts': {}}
    else:
        result = _decide(state, cfg)
        result['comparison'] = comparison(state, cfg)
        state['admission'] = result
    result = state['admission']
    state['views']['admission'] = {
        'title': '开拍准入', 'rows': result['rows'], 'metrics': result['metrics'],
        'lots': result['lots'], 'sessions': result['sessions'], 'audit': result['audit'],
        'evidence': result['evidence'], 'comparison': result.get('comparison', []),
        'policy_label': result['policy_label'], 'policy': result['policy'],
        'outside_packages': result.get('outside_packages', []),
        'as_of': result.get('as_of', ''),
    }


def check_switch(previous, candidate):
    """切换前校验：已开拍货包不能退出可竞价范围、不能换场次或依据，也不能重复出现。"""
    if not previous or not candidate:
        raise BusinessError('NOT_READY', '准入结果尚未生成')
    previous_rows = previous.get('board', [])
    candidate_rows = candidate.get('board', [])
    previous_tickets = [row['ticket'] for row in previous_rows]
    candidate_tickets = [row['ticket'] for row in candidate_rows]
    if len(candidate_tickets) != len(set(candidate_tickets)):
        raise BusinessError('DUPLICATE_PACKAGE', '切换后同一货包重复出现')
    dropped = sorted(set(previous_tickets) - set(candidate_tickets))
    if dropped:
        raise BusinessError(
            'FROZEN_PACKAGE_DROP',
            '已开拍货包不能改换或退出可竞价范围：' + '、'.join(dropped),
            409,
        )
    candidate_by_ticket = {row['ticket']: row for row in candidate_rows}
    moved = sorted(
        ticket for ticket in previous_tickets
        if (candidate_by_ticket[ticket].get('session_id') != next(
                row.get('session_id') for row in previous_rows if row['ticket'] == ticket)
            or candidate_by_ticket[ticket].get('evidence_id') != next(
                row.get('evidence_id') for row in previous_rows if row['ticket'] == ticket))
    )
    if moved:
        raise BusinessError(
            'FROZEN_PACKAGE_CHANGED',
            '已开拍货包不能改换场次或称重依据：' + '、'.join(moved),
            409,
        )
    return candidate
