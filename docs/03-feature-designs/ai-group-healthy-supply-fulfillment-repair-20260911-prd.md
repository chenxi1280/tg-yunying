# AI 活群健康供给履约修复（2026-09-11）

## 授权、范围与状态

用户授权按本轮修复方案完成设计、实现、验证、正式发布及有证据的精确存量恢复。账号恢复、登录、授权切换、扩大账号池不在范围内。当前为 design complete / implementation in progress；本地 QA、制品、运行版本和 Telegram E4 分别取证。

本合同补充 ai-group-executable-supply-and-fallback-20260910-prd.md 与统一履约引擎合同，保留已冻结目标、原截止、账号窗口、发送频率、成员/C2/权限、内容与远端未知约束。不新增降级、限制或假成功路径。

## 问题口径

不可用账号被过滤是不派发的正确行为；账号放弃行跨群重复，不能当作执行故障或独立账号损失。只有证明其占用正文候选/预约或阻塞健康账号扫描才列为执行根因。当前供给、消息数量目标、独立账号覆盖分列；历史冻结需求不缩小。ready 的 next_eligible_at=NULL 允许被选择，不以补时间字段作为修复。

2026-09-11 e78472ac 线上只读证据：Planner 178–241 秒/轮且存在群内容锁冲突；点赞参与选择校验异常；正文时间窗口/共享占用拒绝；C2发送后拦截；生成合同错误、Provider unknown；coverage unknown 对账未闭合。上述错误次数不等于去重义务缺口，各切片需按当前原身份定位并用反例验证后修改，不预设全部是代码缺陷。

## 行为与验收

1. 候选：统一当前健康、目标准入、原义务open、合法时段判断；SQL LIMIT前过滤不可执行账号；游标可跨过不可执行前缀并回绕。保留原任务/账号/群/日/epoch身份；原义务关闭不能继续创建skip-only Action。未物化须能区分真实等待与漏选，健康候选不能饥饿。
2. Planner：分阶段测量真实正文创建数、事实读取/候选/预约耗时和冲突；按瓶颈局部优化。群内容/账号资格锁忙沿原资源重试机制处理，其他任务独立推进；不得终结为生成合同错误。缩短持锁事务不削弱提交前版本、身份、配额复核，统一锁顺序。并发验证无重复预约、状态覆盖、死锁。
3. 时间与占用：预约与claim遵循同一原窗口/来源/间隔/截止；推迟后重新求交。只有可证明未调用且原义务有效才释放失效预约重排；原窗口耗尽明确结算缺口。真实remote在途/unknown保留，不从租约到期推断远端结束。
4. 准入与拦截：成员与GroupBotAdmission使用实际目标canonical身份；成员、C2、can_send分别取证。历史probe按同账号同目标的有效最新事实解释，不把失败次数当当前状态。受拦截账号只推进其合法准备，其他ready继续；真实群管协议不支持或外部权限拒绝明确blocked，不伪造完成。
5. 生成：修复冻结输入在Planner/Job/Dispatcher间的实际错配。既有topic_only与应急开关继续生效；normal failed/emergency_pending必须关联原Action与选择/远端事实，不误判已完成应急。资源锁冲突保留原可恢复工作，不作为合同错误；真实合同错误继续暴露。纯Provider未知仅按既有授权策略接续，Telegram未知禁止替代或重放；晚到结果不得覆盖已选择内容。
6. 存量：有消息事实但投影缺失按原身份修复；有正面未调用证明且原窗有效才正式重排；已调用未知保持对账；过期不补发。先preview精确ID/count/hash，apply复核旧值/运行SHA/审计，独立readback并保持邻居及unknown身份/调用证据。无证据则不apply。
7. 点赞：冻结participation与journey的eligible集合、数量及版本必须一致；不得删除校验或静默截断，不改已调用选择。固定原失败样本并核对规划耗时影响。

## 交付闸门

先PRD后代码，代码入口同步索引。单文件500行、函数50非空行；超限旧入口局部提取，不顺带重构。后端用现有venv，每批60秒硬超时；集成测试只连tg_yunying_test，真实PostgreSQL并发单列。通过定向回归、编译、前端构建及diff检查后冻结候选、本地linux/amd64构建直传（不启用Actions发布）；安装只派发一次并独立读回，成功后精确清理上一轮镜像。随后逐10任务按Task→ledger/coverage→Action→Attempt/Gateway→typed消息及质量/覆盖验收。未达原到期目标如实报告failed/unproven，外部阻塞分项，不以健康/发布通过宣称修复全部业务。

## 已定位修复切片：非内容修订后的策略绑定

13:13同步必需关注频道使西安/郑州楼凤/郑州学生会Task revision=2，仍仅有同epoch revision=1内容绑定；新Job无window时按当前revision读绑定而失败，指纹对应task_ai_content_policy_binding_missing。专用群配置API也存在先activate后增加prejoin revision的同类顺序缺陷。

非内容的预关注更新须在最终revision上绑定原已批准策略，和Task更新同事务。只允许相邻revision、同tenant/task/epoch、相同policy/routes/attestation集合/实际scope，验证原binding evidence hash及有效attestation；为新revision追加同证据与原到期的attestation successor，保留旧binding/attestation/window/unknown。禁止借此更换政策、扩大routes、延长证明有效期或获取新权限。缺原证据、范围/政策变化或漂移明确失败。历史三Task使用同一服务preview/hash/apply/audit/readback补齐，不把当前Job改成旧revision，不直接复活失败Action。

## 已定位修复切片：点赞来源修订身份

旧capacity allocation仅在精确source_identity仍存在时才能作为当前候选保留。普通消息source_identity包含source revision；消息编辑后不能用相同channel_message_id把旧revision选人迁入新revision，否则旧数量50可能超过新需求48并触发journey_participation_selection_invalid。相册仍按原album逻辑身份映射代表消息。保留旧epoch、portfolio及已调用/unknown事实，不截断旧选人或改写旧义务，新revision遵循自己的冻结参与需求和原正式预算。

存量修复可显式指定 `restore_original_references=true`：仅在当前证明引用已漂移、原绑定的完整证明仍通过原hash/当前scope/有效期校验且policy/routes不变时恢复原证据的successor引用。该选项进入preview指纹和审计；正常配置更新不启用，不把现有错误引用当新授权。

预关注字段在Pydantic导出中被排除，是否变更须比较Task独立字段，不能从model_dump键推断；同时修改非内容字段仍须保证最终revision的原授权绑定一致，内容授权字段变化则走原新授权校验。
