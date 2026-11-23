# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 取号单拆分（纯增量）

新增两张表，启动时自动创建，不影响旧版应用：

- `case_tickets`：从档案 JSON 拆出的取号单，按「档案 + 单号」唯一，是切换后查询接口与回放的取数来源。
- `migration_state`：单行迁移状态，记录回填断点（`checkpoint`）与阶段（`backfill` → `verified` → `cutover`）。

迁移步骤（backend 容器内执行）：

1. 部署新版：新建档案双写档案 JSON 与新表；查询接口 `/cases/{id}/tickets[/单号]` 先查新表，未回填的档案自动回退档案 JSON，迁移期间旧版并发新建的档案也能查到。
2. `python -m scripts.migrate_tickets backfill --batch-size 200` 分批回填旧档案。单档案事务，失败后断点停留在上次进度，重跑续传；重复写入按单号去重，不会重复存单。
3. `python -m scripts.migrate_tickets verify` 核齐每个档案的单号与内容，不一致会逐条列出。
4. `python -m scripts.migrate_tickets cutover` 复核一致后切换：查询接口与回放均从新表取数。切换时旧版照常写入，其档案由查询回退和收尾回填覆盖。
5. 如需回退：`python -m scripts.migrate_tickets rollback` 切回旧口径。档案 JSON 始终完整双写，旧版可直接读全迁移期间新建的档案。
