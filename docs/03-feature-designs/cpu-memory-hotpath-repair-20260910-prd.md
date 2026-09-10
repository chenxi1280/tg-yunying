# CPU/内存热点修复合同（2026-09-10）

## Intake与范围

用户授权修复诊断问题1、3、4。L3；目标减少Planner大字段读取、锁内查询成本与单轮重复计算，保持原统计、资格、防重、时效和调度容量。base=85469be6，生产观察仍e454c82a；最新base含其他已合并覆盖义务修复，单独列入发布范围。隔离工作区codex/cpu-memory-hotpaths-20260910；本任务为merge_owner，主目录未跟踪文档保持不动。

## 反向检查与设计

1. `engagement_natural_opportunity._managed_actions`装载整租户当天完整Action，Python再按group_id筛选；5814条payload/result存储态46,816,163bytes，实际只需五字段结果541,016bytes。改为JSON标量group_id/visibility与status、executed_at、scheduled_at投影，保留Python的int(group_id or 0)、状态与时间算法。外部turn只读last_event_at，unowned只读observed_at。不得加Task.id或LIMIT改变同租户同群跨任务统计；不得悄悄忽略非法group_id。保持原查询可见性与事务边界。暂不对未归一化历史JSON加表达式索引或改用其他group身份。
2. AI记忆exact/group-exact/template仅使用命中id，改成ID Row投影，减少查询结果反序列化/ORM对象进入Session；用于发送的记忆实体仍实时完整读取。保留所有tenant/account/group/status/十日与五分钟窗口、排除当前ID、排序和预占唯一冲突处理。
3. 一次_find_duplicate中的相似与语义阶段，使用同一次窗口查询的只读结果，按原顺序/阈值执行。每次_find_duplicate都会重新取得窗口；已有DuplicateMemoryBatch保持原增量刷新规则，调用前exclude_id路径不使用跨请求缓存。一次判定采用一个查询可见快照，不能把同一次判定中的偶然并发提交当作额外保证；原有预占唯一约束与发送前重新核验仍在。新增并发/前后请求变化回归。消除重复扫描，不增大2秒/5秒轮询、不降低并发、不新增Redis或等待回退。
4. 现有Gateway前call-issued提交、账号探测后冻结状态锁、Planner任务/wake锁与Dispatcher完成事件均保留。本批通过缩短锁内读取/计算修复长事务热点，不在缺少完整依赖版本链时移动业务计算跨事务；发布后仍有锁链则以新证据回到诊断，不宣称所有DB长查询已经消失。

## 可实施性、数据与安全自检

接口对前端/API无变化，无迁移，无历史数据改写。统计证据字典字段、数量、状态、可见性、时间边界、版本/哈希计算全部相同；不减少责任范围。scalar JSON在PG/SQLite均保留值类型；非法值继续显式报错。新数据容器使用不可变投影，Session不持有完整Action或只为ID查重的记忆对象。查询错误原样传播。原Task/Action/Attempt/Gateway状态和unknown边界不变，不能通过跳过防重/冻结核验降低耗时。

## QA与发布验收

- 新旧presence证据逐字段比较：跨租户/同群多Task/其他群/各状态/visibility/NULL/时间边界/数字字符串group_id/非法值；证明SQL未读取完整payload/result。
- 查重exact/group/template窗口及id一致，tenant/account隔离和唯一冲突不退化；无匹配时窗口只读一次，两次调用之间新记忆仍能命中；保留发送前exclude自身并检查其余消息。
- 每个后端pytest进程硬超时60秒；真实PG验证JSON类型和Row投影/查询计数。自审无新全局缓存和隐藏fallback。
- 先PRD→实现→审查→定向QA→Release Gate→master/release→本地镜像直传。生产保留34项运行覆盖参数指纹；应用SHA、迁移、合同、容器与静态站点独立读回。
- 修复收益分层：读取规模/查询次数（确定性）；CPU、RSS+Swap、Planner轮耗时（同口径采样且声明重启影响）；任务领取/截止与真实远端事实（不得由资源下降代替）。预估100—300MiB不是承诺。
- 无新迁移；可回退本批代码到本次base（不能回退0232前版本）；失败部署保留证据并定位具体阶段，不自动重放。

Product Design Complete：complete（限定以上确定性热点，无全面调度重写）。
