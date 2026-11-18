from decimal import Decimal, ROUND_HALF_UP
from app.coerce import money

def settle(pair):
    billable=(Decimal(pair['net_kg'])*(1-Decimal(pair['rate']))).quantize(Decimal('.001'),rounding=ROUND_HALF_UP)
    return {**pair,'billable_kg':str(billable),'amount':money(billable*Decimal(pair['price']))}

def apply(state):
    admission=state.get('admission') or {}
    # 未过准入的货包单列，不上竞价板；未纳入准入清单的票号不受影响，保持原有结算范围。
    held={row['ticket']:row for row in admission.get('held',[])} if admission else {}
    # 上板货包按准入确认的称重依据结算（复秤优先）；未被准入改判的版本仍用配对重量。
    basis={(row['ticket'],str(row['version'])):row for row in admission.get('board',[])} if admission else {}
    eligible=[p for p in state['pairs'] if p['ticket'] not in held]
    state['settlements']=[]
    for pair in eligible:
        row=basis.get((pair['ticket'],str(pair['version'])))
        if row and row.get('net_kg'):
            pair={**pair,'net_kg':row['net_kg'],'gross_kg':row['gross_kg'],'tare_kg':row['tare_kg'],
                  'basis_evidence':row['basis_evidence'],'evidence_id':row['evidence_id'],
                  'session_id':row['session_id']}
        state['settlements'].append(settle(pair))
    state['unadmitted']=[{'ticket':t,'reason':row.get('blockers_text') or row.get('status','未准入')}
                         for t,row in sorted(held.items())]
    total=sum((Decimal(p['amount']) for p in state['settlements']),Decimal(0))
    state['views']['auction']={'title':'扣杂结算','rows':state['settlements'],
        'unadmitted':state['unadmitted'],
        'metrics':{'版本票':len(state['settlements']),'未准入货包':len(state['unadmitted']),'金额_CNY':money(total)}}
