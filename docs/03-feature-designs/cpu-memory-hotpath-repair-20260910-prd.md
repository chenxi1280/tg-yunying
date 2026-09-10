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

## 第二轮：上线反查发现的数据库残留（resync，2026-09-10 17:50）

第一轮1e9358f8已完成发布和运行验证，Planner大JSON解码热点消失；不代表数据库问题闭合。新证据：Listener在`engagement_unowned_activity._owned_account_id`等待，远端消息ID全状态查询不能使用仅success的部分索引，EXPLAIN全表扫描ExecutionAttempt、cost约104106；生产活群2818的recent记忆查询为取120条先读取/排序预计12470条，cost约10549；retention候选查询将非空布尔EXISTS包在COALESCE内，优化器保留数十个关联子计划，总cost约18.21亿。只读EXPLAIN反查去除EXISTS外层COALESCE后可转反连接，总cost约162.7万；这只是估算成本，不能作耗时承诺。

第二轮合同：

1. 为`execution_attempts(remote_message_id, action_id)`补全状态索引，保留原success部分索引及全部归属判定；Listener只加载需要的Action，不再加载未使用的Attempt对象。不得把状态缩成success，不得按消息ID跨peer误判归属，不得改变无归属后的观察流程。
2. 为`ai_group_message_memory(tenant_id, group_id, planned_at DESC)`增加排序索引；recent用途分别只投影topic/teacher或normalized/raw字段，原状态集合、120/调用方limit、先后顺序、空值和内部提示词过滤不变，不新增缓存。以实际运行群的EXPLAIN与本地规模测试校验有序limit访问。
3. retention的EXISTS本身恒为非NULL布尔，使用直接NOT EXISTS以允许反连接；只有nullable最近成功时间判定保留COALESCE。所有保护引用、success时间语义、精确批次顺序、锁与删除前复查均保留；不提前limit缩小候选，不取消任何保护条件，不自动修改历史数据。真实PG覆盖引用/并发保护和新旧候选等价。
4. 两个索引使用新的向前迁移并在PG并发创建；已存在索引必须核对有效/ready和定义，冲突或无效状态显式失败，不自动drop/rebuild。无业务字段迁移与线上手工状态改写。发布前冻结SHA与迁移头，保留第一轮34项配置；成功后再清理上一轮镜像。

第二轮Product Design Complete：complete（以上最小SQL/索引改动）；状态resync已同步开发与QA。上线若仍有慢查询/锁链须继续返回具体路径诊断，不能以资源下降声明全部修复。

## 第三轮：预关注远端调用持有事务（resync）

生产1129407a已包含第二轮改动：归属点查4.1ms，真实活群recent120条55.4ms，0233索引valid/ready；但仍出现279—296秒idle-in-transaction。只读线程栈确认多个Dispatcher停在`task_prejoin_channels._follow_parallel`等待Gateway，父Session的最后查询为预关注事实读取。

第三轮只修改该路径的事务边界：读取频道列表、已关注事实后，用不可变快照保存远端所需账号ID/会话密文及Action认领身份、Task生命周期，提交准备事务，再执行既有并行关注；线程不得读取ORM对象，从而避免commit过期加载重新开启事务。远端返回后重新锁定并刷新Action、读取Task生命周期；只有status/owner/token/account/task epoch及Task状态/epoch/退役标记均未变化才能更新Action并继续原调用链。认领或生命周期变化时保留确认成功的关注事实，显式记录停止旧调用，跳过旧Action终态投影；运行资源仍由现有精确owner作用域释放。失败结果保持原有错误与重试语义，异常不伪装成功，不增加超时、重试、降并发或历史状态改写。

成功事实按原账号/目标群/频道身份幂等写入，不能因认领改变而丢失；Action结果合并使用刷新后的当前值，不能覆盖并发新增字段。无待关注频道不额外提交事务。QA覆盖远端期间Session无事务、commit过期对象不被线程读取、部分成功复用、认领改变/任务暂停后不覆盖或继续发送、异常传播及既有准入流程。第三轮Product Design Complete：complete，范围限定该已证实的远端等待路径；不据此声明全系统所有远端调用均无长事务。
