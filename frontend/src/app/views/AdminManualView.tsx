import React from 'react';
import {
  Alert,
  Card,
  Collapse,
  Divider,
  Space,
  Steps,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import {
  Activity,
  Database,
  LayoutDashboard,
  MessageSquareText,
  RefreshCcw,
  ShieldAlert,
  Smartphone,
  Users,
} from 'lucide-react';

const { Paragraph, Text, Title } = Typography;

const quickStartSteps = [
  {
    title: '完成系统基础配置',
    description: '在系统设置中维护 TG 开发者应用、AI 服务、素材运行配置和后台账号权限；素材日常上传进入素材中心。',
  },
  {
    title: '接入 TG 账号',
    description: '在 TG 账号管理中新增账号，完成验证码、二维码或 2FA 登录，并同步群、频道和联系人资产。',
  },
  {
    title: '进入运营中心',
    description: '先看目标工作台、运营方案和异常聚合，按目标展开关联任务失败和建议动作。',
  },
  {
    title: '处理目标和账号能力',
    description: '在运营中心优先用弹窗或抽屉处理目标授权、账号维护、规则版本和风控提示；复杂流程再带返回位置跳转到对应页面。',
  },
  {
    title: '发起并复盘执行',
    description: '优先用运营方案模板生成任务；需要单次执行时进入任务中心，启动后回到运营中心确认异常是否收敛。',
  },
];

const moduleGuides = [
  {
    key: 'overview',
    icon: <LayoutDashboard size={18} />,
    title: '运营中心',
    scenario: '日常工作台，集中查看目标状态、运营方案、异常聚合和任务失败影响。',
    steps: ['先看目标工作台中的 open issue、失败任务、影响账号和最近失败', '点开目标查看关联任务失败、代表执行项和建议动作', '轻量问题直接在弹窗处理，中等问题在右侧抽屉处理', '复杂登录、批量账号维护、完整规则或大量明细再跳转到对应页面，并保留返回运营中心原位置', '在下半部分查看运营方案模板、关联任务和生成入口', '处理后刷新当前目标异常状态'],
    checks: ['目标异常能定位到关联任务或账号', '弹窗或抽屉关闭后仍停留在原目标和原筛选位置', '复杂跳转返回后能恢复原 issue', '方案调整前有影响预览和确认', '汇总数据展示最近更新时间或 stale 提示', '系统设置只维护底座配置，不处理运营方案'],
  },
  {
    key: 'accounts',
    icon: <Smartphone size={18} />,
    title: 'TG账号管理',
    scenario: '账号资产和账号维护：接入、登录恢复、资产同步、分组、资料初始化、账号安全和可用性查看。',
    steps: ['新增账号并选择登录方式', '按页面提示提交验证码、扫码或 2FA', '同步资料、群、频道和联系人资产', '查看账号分组、完整手机号、开发者应用、代理、资料状态和安全状态', '需要批量处理时先点击资料初始化、设置二步密码或清理登录设备，再在抽屉内选择账号', '查看账号可发送、可监听、可加入、可评论、可修改资料和可读取验证码能力', '需要发送消息或联系人发送时进入消息发送页，需要策略处置时进入风控中心'],
    checks: ['账号状态为在线或可恢复', '开发者应用可用', '代理与验证码任务无未处理异常', '查看或同步验证码已填写原因', '外部设备、2FA 和资料初始化结果已在账号安全记录中确认', '账号管理只展示联系人资产，不在本页发消息', '不可用原因和下次可重试时间可解释'],
  },
  {
    key: 'targets',
    icon: <Users size={18} />,
    title: '运营目标',
    scenario: '把已同步资产整理为可执行的群、频道或联系人目标。',
    steps: ['按目标类型筛选群、频道或私聊', '频道可用 @username、公开链接、邀请加入链接或 peer id 手动添加', '确认目标授权、关联账号和最近同步时间', '从目标详情进入消息发送或任务创建', '目标异常时回到账号或风控中心处理'],
    checks: ['目标已确认或可被账号解析', '频道邀请链接未过期', '目标没有被风控拦截'],
  },
  {
    key: 'message',
    icon: <MessageSquareText size={18} />,
    title: '消息发送',
    scenario: '面向单个或多个目标创建人工消息发送任务。',
    steps: ['选择目标和发送账号范围', '填写消息内容并选择素材', '检查发送时间、重试和审核要求', '提交后在任务列表观察发送状态'],
    checks: ['内容不触发规则拦截', '目标和账号容量足够', '失败任务已查看原因并重试或取消'],
  },
  {
    key: 'materials',
    icon: <Database size={18} />,
    title: '素材中心',
    scenario: '统一维护表情包、头像包、图片、文件和组合消息素材。',
    steps: ['查看素材总览、表情包、头像包和图片/文件分类', '上传单个或批量素材并填写标签分组', '检查 TG 缓存状态、待缓存数量和最近失败原因', '被消息发送、任务中心、规则中心或账号资料初始化引用后，只禁用或新增版本，不物理删除'],
    checks: ['素材类型和标签分组清楚', '缓存账号和源媒体缓存状态可解释', '上传或编辑素材需要素材权限', '系统设置只维护素材运行配置，不承担日常素材管理'],
  },
  {
    key: 'tasks',
    icon: <Activity size={18} />,
    title: '任务中心',
    scenario: '创建和追踪 AI 活跃群、转发监听群、频道浏览、频道点赞和频道评论任务的执行事实。',
    steps: ['选择任务类型和运营目标，或直接粘贴群聊/频道入口', '使用动态向导填写任务类型对应字段，默认节奏可直接创建，必要时展开高级设置', '在确认页查看容量、规则、风控、账号可用性、准入预览和 warning', '创建并启动，或先保存草稿再启动', '任务会先检查账号是否已关注或已加入', '未满足账号先按抖动节奏关注或加入目标', '已满足账号先进入主互动，准备成功账号后续追加执行', '在任务详情查看准入前置、执行项、attempt、账号分配和失败原因'],
    checks: ['预检查通过', '账号池容量满足任务规模', '频道任务至少有账号已关注或可通过链接加入', '关注失败的账号不会进入浏览、点赞或评论主阶段', '失败会汇总到运营中心目标异常', '暂停、继续、停止、重试都有审计记录'],
  },
  {
    key: 'listeners',
    icon: <RefreshCcw size={18} />,
    title: '监听中心',
    scenario: '查看转发、AI 活跃和频道任务关联的监听状态。',
    steps: ['按频道或群查看监听账号', '观察事件积压、最后事件和错误', '发现空监听时回到任务或账号中心补齐监听账号', '恢复后刷新确认事件继续流转'],
    checks: ['监听账号在线', '源群或频道可读取', '积压没有持续上升'],
  },
  {
    key: 'rules',
    icon: <ShieldAlert size={18} />,
    title: '规则中心',
    scenario: '维护过滤、转换、路由、命中统计和规则版本。',
    steps: ['在规则集中新增或编辑规则', '使用规则测试检查样本文本', '发布版本后再绑定到任务', '通过命中统计回看拦截、转换和路由效果'],
    checks: ['使用已发布版本', '关键词、白名单和路由条件不互相冲突', '高风险修改写清原因'],
  },
  {
    key: 'risk',
    icon: <ShieldAlert size={18} />,
    title: '风控中心',
    scenario: '处理账号风险、账号安全、目标能力、限流、冷却、代理异常和处置队列。',
    steps: ['先看风险总览和账号评分', '查看账号安全、外部设备、2FA 和资料初始化相关提示', '处理待处置队列中的异常', '需要时调整策略并填写原因', '回到任务中心重试受影响任务'],
    checks: ['账号不过度集中使用', '目标慢速模式和禁止发言状态已识别', '账号安全异常已处理或登记原因', '代理异常不会继续放大任务失败'],
  },
  {
    key: 'archive',
    icon: <Database size={18} />,
    title: '归档中心',
    scenario: '沉淀群消息、执行记录和可复盘材料。',
    steps: ['选择目标或群资产创建归档', '查看归档详情和上下文', '需要外部复盘时导出', '异常归档可重新执行'],
    checks: ['归档对象正确', '导出前确认是否包含敏感内容', '失败归档已重新运行或记录原因'],
  },
  {
    key: 'data',
    icon: <Database size={18} />,
    title: '运营数据与审计',
    scenario: '复盘任务效果、AI 用量、失败项和关键操作留痕。',
    steps: ['在运营数据查看任务、账号和模型用量', '先看汇总口径和最近更新时间，再按目标、任务、账号或 action 下钻', '用筛选条件定位失败或异常执行', '在审计记录查看登录、发送、规则和风控操作', '导出前确认权限和用途'],
    checks: ['数据口径与任务时间一致', '汇总延迟有 stale 或更新时间提示', '敏感审计仅授权人员查看', '导出审计记录已填写原因', '异常操作能回溯到操作者和原因'],
  },
];

const recentFeatureGuides = [
  {
    title: '账号安全加固',
    tag: 'TG账号管理',
    detail: '进入账号详情的账号安全页，可刷新设备和 2FA 状态，清理外部设备，设置 2FA，并查看最近安全批次结果。',
  },
  {
    title: '批量资料初始化',
    tag: 'TG账号管理',
    detail: '在账号列表勾选多个账号后，系统用一次 AI 请求生成整批昵称、简介和 username 候选；可填写命名风格提示、手工编辑预览，AI 不可用时会用本地随机昵称兜底并展示原因。',
  },
  {
    title: '任务内目标输入',
    tag: '任务中心',
    detail: '创建任务时可以选择已有运营目标，也可以直接粘贴群聊或频道 @username、公开链接、邀请链接或 peer id，系统会自动创建或复用运营目标。',
  },
  {
    title: '账号-目标准入前置',
    tag: '任务中心',
    detail: '频道和群聊任务启动前会先检查账号是否已关注或已加入；已满足账号先执行，未满足账号先准备，成功后再追加进入主互动。',
  },
  {
    title: '手机号展示',
    tag: 'TG账号管理',
    detail: '账号相关列表优先展示完整手机号；历史数据没有完整手机号时才使用兼容字段兜底。',
  },
  {
    title: '频道评论不可用',
    tag: '任务中心',
    detail: '频道帖子无法解析到评论区、频道未绑定讨论组或账号无法进入讨论组评论时，会归因为评论区不可用；可先改用浏览/点赞，或修复讨论组权限后重试评论。',
  },
  {
    title: '运营中心日常入口',
    tag: '运营中心',
    detail: '登录后先看运营中心目标工作台，默认按目标聚合 open issue 和失败任务；点开后查看关联任务失败、代表执行项和建议动作。建议动作优先用弹窗或抽屉处理，复杂流程跳转时保留返回位置。',
  },
  {
    title: '运营方案模板',
    tag: '运营中心',
    detail: '运营中心下半部分展示方案模板，可生成任务草稿、生成并启动、暂停、恢复、复制和调整关联任务；应用到运行中任务前必须先看影响预览。',
  },
  {
    title: '素材中心',
    tag: '素材中心',
    detail: '素材上传、批量上传、表情包、头像包、图片/文件、缓存健康和引用边界统一进入一级素材中心；系统设置只保留素材运行配置。',
  },
  {
    title: '任务创建动态向导',
    tag: '任务中心',
    detail: '创建任务时按任务类型动态展示字段，默认快速创建，高级设置折叠；确认页集中展示容量、规则、风控、账号可用性、准入预览和 warning。',
  },
  {
    title: '账号资产与可用性',
    tag: 'TG账号管理',
    detail: '账号中心展示账号资产、完整手机号、分组、登录状态、资料/安全状态、可发送、可监听、可加入、可评论、可修改资料、可读取验证码、剩余容量、不可用原因和下次可重试时间；发送动作进入消息发送页。',
  },
  {
    title: '数据汇总与延迟',
    tag: '运营数据',
    detail: '运营中心和任务中心列表默认读取汇总数据；详情按目标、任务、账号或 action 下钻。看到 stale 或更新时间较旧时先刷新或进入详情确认。',
  },
  {
    title: '导航升级',
    tag: '系统设置',
    detail: '菜单统一使用“运营中心”；素材中心作为一级菜单承载上传、表情包和头像包；AI、提示词、素材运行配置和后台账号权限位于系统设置 Tab。',
  },
];

const taskTypes = [
  ['AI 活跃群', '在群内按上下文生成自然对话，适合保持群活跃度。'],
  ['转发监听群', '监听源群消息，经规则过滤、转换和路由后发送到目标群。'],
  ['频道浏览', '为频道消息安排查看动作；未关注账号先关注频道，关注成功后再执行浏览。'],
  ['频道点赞', '为频道消息安排点赞动作；未关注账号先关注频道，关注成功后再执行点赞。'],
  ['频道评论/回复', '在频道讨论区执行评论或回复；主互动只使用已关注或刚关注成功的账号。'],
];

const exceptionPlaybook = [
  {
    color: 'gold',
    children: (
      <>
        <Text strong>运营中心出现目标异常：</Text>
        先按目标展开关联任务失败和代表执行项；轻量问题直接在弹窗处理，中等问题在抽屉处理，复杂流程再跳转账号、目标、规则、风控或任务详情并保留返回位置。
      </>
    ),
  },
  {
    color: 'red',
    children: (
      <>
        <Text strong>登录或账号不可用：</Text>
        先在 TG 账号管理查看登录状态、验证码待处理、开发者应用和代理状态，再执行同步或重新登录。
      </>
    ),
  },
  {
    color: 'orange',
    children: (
      <>
        <Text strong>任务创建预检查失败：</Text>
        先看预检查提示，通常从目标权限、账号容量、规则版本、AI 服务和风控限制四处排查。
      </>
    ),
  },
  {
    color: 'blue',
    children: (
      <>
        <Text strong>监听没有新事件：</Text>
        检查监听中心的监听账号、源群读取权限和事件积压，再回到任务详情确认绑定关系。
      </>
    ),
  },
  {
    color: 'purple',
    children: (
      <>
        <Text strong>内容被规则拦截：</Text>
        在规则中心用同样文本做规则测试，确认命中条件后调整规则或修改任务内容。
      </>
    ),
  },
  {
    color: 'green',
    children: (
      <>
        <Text strong>执行结果需要复盘：</Text>
        先看运营中心的目标摘要，再看任务详情、运营数据、归档中心和审计记录，按时间线确认每一步结果。
      </>
    ),
  },
  {
    color: 'gray',
    children: (
      <>
        <Text strong>汇总数据延迟：</Text>
        看到 stale 标记或最近更新时间较旧时，先刷新汇总；仍不确定时按目标、任务、账号或 action 下钻详情，不直接用首页数字判断任务无异常。
      </>
    ),
  },
  {
    color: 'cyan',
    children: (
      <>
        <Text strong>账号未满足目标准入：</Text>
        频道任务会先检查关注状态，AI 活跃群和转发源群、目标群会先检查加入状态；未满足账号生成准入前置动作，只补齐未满足账号；全部准备失败时不会进入浏览、点赞、评论、群发言或转发主阶段。
      </>
    ),
  },
  {
    color: 'red',
    children: (
      <>
        <Text strong>频道评论区不可用：</Text>
        确认消息 ID 属于频道帖子、频道已绑定讨论组，并且执行账号可进入讨论组评论；不满足时先处理目标权限或改用浏览/点赞任务。
      </>
    ),
  },
];

export default function AdminManualView() {
  return (
    <div className="admin-manual">
      <Alert
        type="info"
        showIcon
        message="管理员操作手册"
        description="本手册按登录后的真实菜单组织，适合平台管理员、运营主管和运营人员日常接入账号、确认目标、创建任务、排查异常和复盘数据。"
      />

      <Card className="panel manual-section" title="日常操作顺序" extra={<Tag color="blue">推荐流程</Tag>}>
        <Steps className="manual-steps" current={-1} items={quickStartSteps} />
      </Card>

      <div className="manual-grid">
        <Card className="panel manual-section" title="上线前检查">
          <div className="manual-checklist">
            {[
              '系统设置中的 TG 开发者应用和 AI 服务处于可用状态。',
              'TG 账号已登录并同步资产，账号健康分、代理状态和账号安全状态正常。',
              '运营目标已确认；频道目标可用 @username、公开链接、邀请加入链接或 peer id 添加。',
              '规则中心已有发布版本，任务绑定的是已发布规则集。',
              '风控中心没有未处理的高风险账号、目标或代理异常。',
            ].map((item) => (
              <div className="manual-check-item" key={item}>
                <Activity size={16} />
                <span>{item}</span>
              </div>
            ))}
          </div>
        </Card>

        <Card className="panel manual-section" title="任务类型选择">
          <Space direction="vertical" size={10} className="manual-task-list">
            {taskTypes.map(([name, detail]) => (
              <div className="manual-task-row" key={name}>
                <Tag color="geekblue">{name}</Tag>
                <Text>{detail}</Text>
              </div>
            ))}
          </Space>
        </Card>
      </div>

      <Card className="panel manual-section" title="最近更新功能" extra={<Tag color="green">已纳入流程</Tag>}>
        <Space direction="vertical" size={10} className="manual-task-list">
          {recentFeatureGuides.map((feature) => (
            <div className="manual-task-row" key={feature.title}>
              <Tag color="green">{feature.tag}</Tag>
              <Space direction="vertical" size={2}>
                <Text strong>{feature.title}</Text>
                <Text>{feature.detail}</Text>
              </Space>
            </div>
          ))}
        </Space>
      </Card>

      <Card className="panel manual-section" title="按菜单操作">
        <Collapse
          className="manual-collapse"
          bordered={false}
          defaultActiveKey={['overview', 'accounts', 'targets', 'tasks']}
          items={moduleGuides.map((guide) => ({
            key: guide.key,
            label: (
              <Space size={10}>
                <span className="manual-collapse-icon">{guide.icon}</span>
                <span>{guide.title}</span>
                <Text type="secondary">{guide.scenario}</Text>
              </Space>
            ),
            children: (
              <div className="manual-module">
                <div>
                  <Title level={5}>操作步骤</Title>
                  <ol>
                    {guide.steps.map((step) => <li key={step}>{step}</li>)}
                  </ol>
                </div>
                <div>
                  <Title level={5}>完成标准</Title>
                  <ul>
                    {guide.checks.map((check) => <li key={check}>{check}</li>)}
                  </ul>
                </div>
              </div>
            ),
          }))}
        />
      </Card>

      <Card className="panel manual-section" title="异常处理速查">
        <Timeline items={exceptionPlaybook} />
        <Divider />
        <Paragraph type="secondary" className="manual-note">
          处理异常时优先保留现场：不要先删除任务或账号。先记录任务 ID、账号、目标、时间和错误提示，再按账号、目标、规则、风控、执行记录的顺序定位。
        </Paragraph>
      </Card>

      <Card className="panel manual-section" title="权限与审计要求">
        <div className="manual-permission-grid">
          <div>
            <Space size={8}>
              <Users size={18} />
              <Text strong>权限</Text>
            </Space>
            <Paragraph>
              后台账号按菜单、按钮和写接口控制。账号添加专员只处理账号接入；运营人员处理任务和目标；平台管理员维护系统配置、权限和高风险处置。
            </Paragraph>
          </div>
          <div>
            <Space size={8}>
              <Database size={18} />
              <Text strong>审计</Text>
            </Space>
            <Paragraph>
              登录、发送、规则发布、风控处置、任务停止、删除和敏感导出都应能在审计记录中追踪到操作者、时间、对象和原因。
            </Paragraph>
          </div>
          <div>
            <Space size={8}>
              <Activity size={18} />
              <Text strong>自动执行</Text>
            </Space>
            <Paragraph>
              AI、监听和 Worker 自动执行前，先确认目标、规则和风控状态；执行中通过任务详情、监听中心和运营数据持续观察。
            </Paragraph>
          </div>
        </div>
      </Card>
    </div>
  );
}
