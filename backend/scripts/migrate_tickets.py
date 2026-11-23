"""取号单拆分迁移工具：分批回填、核对、切换与回退。

在 backend 容器内执行（与 scripts.seed_demo 相同环境）：

    python -m scripts.migrate_tickets status      查看回填断点与迁移阶段
    python -m scripts.migrate_tickets backfill    分批回填旧档案（断点续跑，幂等不重单）
    python -m scripts.migrate_tickets verify      核齐每个档案的单号与内容
    python -m scripts.migrate_tickets cutover     复核一致后切换到新表取数
    python -m scripts.migrate_tickets rollback    切回旧口径（回放改读档案 JSON）

回填以单个档案为事务：失败时断点停留在上一个成功的档案，修复后重跑即可；
同一档案同一单号重复写入会被忽略，重试不会重复存单。切换后档案 JSON 继续
双写，切回旧版仍能读全迁移期间新建的档案。
"""
import argparse
import sys

from sqlalchemy import func, select

from src.config import settings
from src.models.tables import cases, case_tickets, schema
from src.services import tickets as store


def cmd_status(_args):
    with settings.engine.connect() as conn:
        current = store.state(conn)
        total = conn.execute(select(func.count()).select_from(cases)).scalar_one()
        split_cases = conn.execute(select(func.count(func.distinct(case_tickets.c.case_id)))).scalar_one()
        rows = conn.execute(select(func.count()).select_from(case_tickets)).scalar_one()
    print(f"迁移阶段：{current['phase']}（backfill 回填中 / verified 已核齐 / cutover 已切换）")
    print(f"回填断点：档案编号 {current['checkpoint']}")
    print(f"档案总数：{total}；已拆分档案：{split_cases}；新表取号单：{rows}")
    if split_cases < total:
        print(f"提示：{total - split_cases} 个档案尚无新表记录（含无取号单的档案），查询会自动回退档案 JSON。")
    return 0


def cmd_backfill(args):
    if args.batch_size < 1:
        print('每批档案数须大于 0。')
        return 1
    done = rows = batches = 0
    start = args.from_id  # None 表示从断点开始
    while True:
        with settings.engine.connect() as conn:
            current = start if start is not None else store.state(conn)['checkpoint']
            batch = conn.execute(select(cases.c.id, cases.c.payload)
                                 .where(cases.c.id > current).order_by(cases.c.id)
                                 .limit(args.batch_size)).mappings().all()
        if not batch:
            break
        for case in batch:
            try:
                with settings.engine.begin() as conn:
                    rows += store.backfill_case(conn, case)
            except Exception as exc:
                print(f"回填中断：档案 {case['id']} 写入失败（{exc}）。")
                print(f"已完成 {done} 个档案，断点保留在上次进度，修复后直接重跑即可续跑。")
                return 1
            done += 1
        batches += 1
        start = batch[-1]['id']  # 从本批末尾继续，避免 --from-id 重扫时原地打转
        print(f"第 {batches} 批：回填至档案 {batch[-1]['id']}（本批 {len(batch)} 个档案）")
        if args.max_batches and batches >= args.max_batches:
            break
    tail = '已追平最新档案。' if not batch else '仍有旧档案待回填，再次执行即可续跑。'
    print(f"本次回填 {done} 个档案、{rows} 张取号单。{tail}")
    return 0


def run_verify():
    problems = []
    checked = 0
    with settings.engine.connect() as conn:
        batch = conn.execute(select(cases.c.id, cases.c.payload).order_by(cases.c.id)).mappings().all()
        for case in batch:
            problems.extend(store.verify_case(conn, case))
            checked += 1
    return checked, problems


def report(problems):
    for line in problems[:20]:
        print(f" - {line}")
    if len(problems) > 20:
        print(f" ……其余 {len(problems) - 20} 处从略")


def cmd_verify(_args):
    checked, problems = run_verify()
    if problems:
        print(f"核对未通过：{checked} 个档案中发现 {len(problems)} 处不一致：")
        report(problems)
        print('请先重跑 backfill 补齐，再重新核对。')
        return 1
    with settings.engine.begin() as conn:
        if store.state(conn)['phase'] == store.PHASE_BACKFILL:
            store.set_phase(conn, store.PHASE_VERIFIED)
    print(f"核对通过：{checked} 个档案的单号与内容一致，可以切换。")
    return 0


def cmd_cutover(_args):
    checked, problems = run_verify()
    if problems:
        print(f"切换中止：{len(problems)} 处不一致，请先回填并核对：")
        report(problems)
        return 1
    with settings.engine.begin() as conn:
        store.set_phase(conn, store.PHASE_CUTOVER)
    print(f"已切换：{checked} 个档案核齐，查询接口与回放均从新表取数。")
    print('档案 JSON 继续双写，切回旧版仍能读全迁移期间新建的档案。')
    print('提示：迁移窗口内旧版新建的档案，请再跑一次 backfill 收尾。')
    return 0


def cmd_rollback(_args):
    with settings.engine.begin() as conn:
        store.set_phase(conn, store.PHASE_VERIFIED)
    print('已切回旧口径：回放改读档案 JSON，新表数据保留。')
    print('档案 JSON 始终完整双写，旧版应用可直接读取全部档案。')
    return 0


def main():
    parser = argparse.ArgumentParser(prog='migrate_tickets', description='取号单拆分迁移工具')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='查看回填断点与迁移阶段').set_defaults(func=cmd_status)
    backfill = commands.add_parser('backfill', help='分批回填旧档案，断点续跑，幂等不重单')
    backfill.add_argument('--batch-size', type=int, default=200, help='每批档案数（默认 200）')
    backfill.add_argument('--max-batches', type=int, default=0, help='本次最多批数（默认不限）')
    backfill.add_argument('--from-id', type=int, default=None,
                          help='从指定档案编号之后重扫（默认取断点；用于补扫，断点不会回退）')
    backfill.set_defaults(func=cmd_backfill)
    commands.add_parser('verify', help='核齐每个档案的单号与内容').set_defaults(func=cmd_verify)
    commands.add_parser('cutover', help='复核一致后切换到新表取数').set_defaults(func=cmd_cutover)
    commands.add_parser('rollback', help='切回旧口径').set_defaults(func=cmd_rollback)
    args = parser.parse_args()
    schema.create_all(settings.engine)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
