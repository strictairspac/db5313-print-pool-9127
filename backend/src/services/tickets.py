"""取号单拆分存储。

档案 JSON 仍完整保留取号单，旧版应用回滚后可直接读取；新表 case_tickets
按「档案 + 单号」唯一，是切换之后查询接口与回放的取数来源。回填断点与
迁移阶段记录在 migration_state 单行表中，全部操作幂等，可安全重试。
"""

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.models.tables import case_tickets, migration_state
from src.utils.time import utc_time

FIELDS = ('ticket_id', 'tenant', 'priority', 'arrival_ms', 'hold_ms', 'wait_budget_ms')
STATE_ID = 1
PHASE_BACKFILL = 'backfill'  # 回填中：回放仍读档案 JSON
PHASE_VERIFIED = 'verified'  # 已核齐：新表与档案 JSON 一致，等待切换
PHASE_CUTOVER = 'cutover'    # 已切换：查询接口与回放从新表取数


def normalize(raw):
    """把档案 JSON 里的一张取号单规整为新表列（与回放口径一致）。"""
    return {
        'ticket_id': str(raw['ticket_id']).strip(),
        'tenant': str(raw['tenant']).strip(),
        'priority': str(raw['priority']).strip(),
        'arrival_ms': int(raw['arrival_ms']),
        'hold_ms': int(raw['hold_ms']),
        'wait_budget_ms': int(raw['wait_budget_ms']),
    }


def _insert(conn, table):
    if conn.dialect.name == 'mysql':
        return mysql_insert(table)
    if conn.dialect.name == 'sqlite':
        return sqlite_insert(table)
    return postgres_insert(table)


def upsert(conn, case_id, rows):
    """幂等写入：同一档案同一单号重复写入时忽略，重试不会重复存单。"""
    if not rows:
        return
    values = [dict(row, case_id=case_id) for row in rows]
    statement = _insert(conn, case_tickets).values(values)
    if conn.dialect.name == 'mysql':
        statement = statement.on_duplicate_key_update(id=case_tickets.c.id)
    else:
        statement = statement.on_conflict_do_nothing(index_elements=('case_id', 'ticket_id'))
    conn.execute(statement)


def fetch(conn, case_id):
    rows = conn.execute(select(case_tickets).where(case_tickets.c.case_id == case_id)
                        .order_by(case_tickets.c.ticket_id)).mappings()
    return [{key: row[key] for key in FIELDS} for row in rows]


def ensure_state(conn):
    statement = _insert(conn, migration_state).values(
        id=STATE_ID, checkpoint=0, phase=PHASE_BACKFILL, updated_at=utc_time())
    if conn.dialect.name == 'mysql':
        statement = statement.on_duplicate_key_update(id=migration_state.c.id)
    else:
        statement = statement.on_conflict_do_nothing(index_elements=('id',))
    conn.execute(statement)


def state(conn):
    row = conn.execute(select(migration_state).where(migration_state.c.id == STATE_ID)).mappings().first()
    return dict(row) if row else {'id': STATE_ID, 'checkpoint': 0, 'phase': PHASE_BACKFILL,
                                  'updated_at': None}


def set_phase(conn, phase):
    ensure_state(conn)
    conn.execute(migration_state.update().where(migration_state.c.id == STATE_ID)
                 .values(phase=phase, updated_at=utc_time()))


def backfill_case(conn, case):
    """回填单个档案并推进断点。两者同事务提交，失败时断点停留在上一成功档案。"""
    rows = [normalize(raw) for raw in (case['payload'] or {}).get('tickets') or []]
    upsert(conn, case['id'], rows)
    ensure_state(conn)
    # 断点只前进不后退：并发或乱序重跑不会丢失已完成进度。
    conn.execute(migration_state.update()
                 .where(migration_state.c.id == STATE_ID,
                        migration_state.c.checkpoint < case['id'])
                 .values(checkpoint=case['id'], updated_at=utc_time()))
    return len(rows)


def effective_tickets(conn, case):
    """先查新表；档案尚未回填（迁移窗口内旧版写入）时回退档案 JSON。"""
    rows = fetch(conn, case['id'])
    if rows:
        return rows, 'split'
    return [normalize(raw) for raw in (case['payload'] or {}).get('tickets') or []], 'payload'


def replay_payload(conn, case):
    """回放取数：切换后从新表取数，未回填的档案回退档案 JSON。"""
    if state(conn)['phase'] != PHASE_CUTOVER:
        return case['payload']
    tickets, _ = effective_tickets(conn, case)
    return dict(case['payload'], tickets=tickets)


def verify_case(conn, case):
    """核对单个档案：新表与档案 JSON 的单号集合和内容须一致。"""
    expected = {}
    for raw in (case['payload'] or {}).get('tickets') or []:
        row = normalize(raw)
        expected[row['ticket_id']] = row
    actual = {row['ticket_id']: row for row in fetch(conn, case['id'])}
    problems = []
    for ticket_id in sorted(expected.keys() - actual.keys()):
        problems.append(f"档案 {case['id']}：新表缺少取号单 {ticket_id}")
    for ticket_id in sorted(actual.keys() - expected.keys()):
        problems.append(f"档案 {case['id']}：新表多出取号单 {ticket_id}")
    for ticket_id in sorted(expected.keys() & actual.keys()):
        diffs = [f"{key} 档案={expected[ticket_id][key]!r} 新表={actual[ticket_id][key]!r}"
                 for key in FIELDS if expected[ticket_id][key] != actual[ticket_id][key]]
        if diffs:
            problems.append(f"档案 {case['id']} 取号单 {ticket_id} 内容不一致（{'；'.join(diffs)}）")
    return problems
