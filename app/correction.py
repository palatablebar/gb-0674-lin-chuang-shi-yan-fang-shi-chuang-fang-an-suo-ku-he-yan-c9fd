"""重归属更正裁决。

裁决口径：只更正凭证覆盖的访视单元（同一受试者同一访视的首次记录与其全部复检），
单元整体在互换对之间交换归属。整人互换会改动凭证未覆盖的历史，故不按人整体互换；
逐条改号会把复检与原始访视拆散，故单元不完整即阻断。更正不能新增受试者或访视。
"""
from collections import defaultdict
from app.errors import BusinessError

def identity(spec):
    """更正身份：同一对受试者+同一组凭证记录即同一更正，重试得到同一批号。"""
    return {'pair': sorted(spec['pair']), 'records': sorted(set(spec['records']))}

def parse_spec(payload):
    if not isinstance(payload, dict):
        raise BusinessError('BAD_CORRECTION', '更正参数须为对象')
    pair = payload.get('pair')
    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) and x.strip() for x in pair):
        raise BusinessError('BAD_CORRECTION', '互换对须为两个受试者编号')
    pair = sorted(x.strip() for x in pair)
    if pair[0] == pair[1]:
        raise BusinessError('BAD_CORRECTION', '互换对须为两个不同受试者')
    records = payload.get('records')
    if not isinstance(records, list) or not records or not all(isinstance(x, str) and x.strip() for x in records):
        raise BusinessError('BAD_CORRECTION', '凭证记录须为非空编号列表')
    reason = str(payload.get('reason', '')).strip()
    if not reason:
        raise BusinessError('BAD_CORRECTION', '请填写更正原因')
    return {'pair': pair, 'records': sorted({x.strip() for x in records}), 'reason': reason}

def validate(records, context, spec):
    """返回阻断清单；为空表示更正范围合法。"""
    blocks = []
    pair = set(spec['pair'])
    for subject in spec['pair']:
        if subject not in context['subjects']:
            blocks.append({'code': 'SUBJECT_UNKNOWN', 'subject': subject,
                           'detail': f'受试者{subject}未入组，更正不能新增受试者'})
    if blocks:
        return blocks
    by_id = {r['id']: r for r in records}
    chosen = set(spec['records'])
    for rid in spec['records']:
        row = by_id.get(rid)
        if row is None:
            blocks.append({'code': 'RECORD_UNKNOWN', 'id': rid, 'detail': '凭证记录不存在或尚未采信'})
        elif row['subject'] not in pair:
            blocks.append({'code': 'PAIR_MISMATCH', 'id': rid,
                           'detail': f"记录归属{row['subject']}，不在互换对内"})
    units = defaultdict(list)
    for row in records:
        if row['subject'] in pair:
            units[(row['subject'], row['visit'])].append(row['id'])
    for (subject, visit), ids in sorted(units.items()):
        covered = [i for i in ids if i in chosen]
        if covered and len(covered) != len(ids):
            missing = ','.join(i for i in ids if i not in chosen)
            blocks.append({'code': 'UNIT_INCOMPLETE', 'subject': subject, 'visit': visit,
                           'detail': f'访视单元须整体更正，否则复检与原始访视分离，缺少{missing}'})
    covered_subjects = {by_id[i]['subject'] for i in chosen if i in by_id}
    if len(covered_subjects) == 1:
        blocks.append({'code': 'SWAP_ONE_SIDED', 'subject': next(iter(covered_subjects)),
                       'detail': '更正须成对互换，单向改号将造成重复首次访视'})
    return blocks

def apply_correction(state, spec):
    """在采信阶段改归属，下游方案、访视、锁库、归档引用同一归属。原始编号保留在映射与快照中。"""
    spec = parse_spec(spec)
    context = state['context']
    blocks = validate(state['records'], context, spec)
    if blocks:
        raise BusinessError('CORRECTION_BLOCKED', '更正范围未通过裁决', 422, {'blocks': blocks})
    other = {spec['pair'][0]: spec['pair'][1], spec['pair'][1]: spec['pair'][0]}
    chosen = set(spec['records'])
    mapping = []
    for row in state['records']:
        row.setdefault('original_subject', '')
        if row['id'] in chosen:
            new_subject = other[row['subject']]
            mapping.append({'id': row['id'], 'visit': row['visit'], 'kind': row['kind'],
                            'old_subject': row['subject'], 'new_subject': new_subject,
                            'old_enrolled': row['enrolled'], 'new_enrolled': context['subjects'][new_subject]})
            row['original_subject'] = row['subject']
            row['subject'] = new_subject
            row['enrolled'] = context['subjects'][new_subject]
    mapping.sort(key=lambda m: m['id'])
    units = {}
    for m in mapping:
        key = (m['old_subject'], m['visit'])
        unit = units.setdefault(key, {'visit': m['visit'], 'from_subject': m['old_subject'],
                                      'to_subject': m['new_subject'], 'records': []})
        unit['records'].append(m['id'])
    affected = [dict(u, records=','.join(u['records'])) for u in units.values()]
    affected.sort(key=lambda u: (u['visit'], u['from_subject']))
    state['correction'] = {'pair': spec['pair'], 'reason': spec['reason'], 'mapping': mapping,
                           'affected_visits': affected, 'records': len(mapping)}
    view = state['views'].get(context['keys'][0])
    if view is not None:
        view['rows'] = [dict(r) for r in state['records']]
        view['metrics']['重归属'] = len(mapping)
