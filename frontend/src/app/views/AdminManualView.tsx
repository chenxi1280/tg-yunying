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
    description: '在系统设置中维护 TG 开发者应用、AI 服务、素材和后台账号权限。',
  },
  {
    title: '接入 TG 账号',
    description: '在 TG 账号管理中新增账号，完成验证码、二维码或 2FA 登录，并同步群、频道和联系人资产。',
  },
  {
    title: '确认运营目标',
    description: '在运营目标中确认可操作的群、频道或私聊对象，目标状态异常时先处理授权或账号能力。',
  },
  {
    title: '配置规则与风控',
    description: '先发布规则集，再检查账号容量、代理、冷却、目标限制和风险处置队列。',
  },
  {
    title: '创建并追踪任务',
    description: '在任务中心选择任务类型、目标、账号范围和执行策略，启动后通过详情、监听中心、数据和审计追踪。',
  },
];

const moduleGuides = [
  {
    key: 'accounts',
    icon: <Smartphone size={18} />,
    title: 'TG账号管理',
    scenario: '账号接入、登录恢复、资料同步、安全加固、资料初始化与容量准备。',
    steps: ['新增账号并选择登录方式', '按页面提示提交验证码、扫码或 2FA', '同步群、频道和联系人', '在账号详情的账号安全页刷新设备和 2FA 状态', '需要批量处理时勾选账号后分别执行资料初始化、设置二步密码或清理登录设备', '检查账号状态、健康分和可操作范围'],
    checks: ['账号状态为在线或可恢复', '开发者应用可用', '代理与验证码任务无未处理异常', '外部设备、2FA 和资料初始化结果已在账号安全记录中确认'],
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
    key: 'tasks',
    icon: <Activity size={18} />,
    title: '任务中心',
    scenario: '创建 AI 活跃群、转发监听群、频道浏览、频道点赞和频道评论任务。',
    steps: ['选择任务类型和运营目标', '使用默认节奏创建，必要时展开高级设置', '创建并启动，或先保存草稿再启动', '频道任务会先判断目标是否已授权且可发送', '已授权且可发送的频道直接进入主互动', '未授权或不可发送时，系统只为未关注账号按抖动节奏补齐关注', '在任务详情查看前置关注、执行项、账号分配和失败原因'],
    checks: ['预检查通过', '账号池容量满足任务规模', '频道任务至少有账号已关注或可通过链接加入', '关注失败的账号不会进入浏览、点赞或评论主阶段', '暂停、继续、停止、重试都有审计记录'],
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
    steps: ['在运营数据查看任务、账号和模型用量', '用筛选条件定位失败或异常执行', '在审计记录查看登录、发送、规则和风控操作', '导出前确认权限和用途'],
    checks: ['数据口径与任务时间一致', '敏感审计仅授权人员查看', '异常操作能回溯到操作者和原因'],
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
    title: '频道目标手动添加',
    tag: '运营目标',
    detail: '频道目标支持填写 @username、公开链接、邀请加入链接和 peer id。邀请加入链接可使用 https://t.me/+xxx 或 joinchat 形式，提交后系统自动识别。',
  },
  {
    title: '频道任务前置关注',
    tag: '任务中心',
    detail: '频道浏览、点赞、评论启动前会先检查频道能力；已授权且可发送的频道直接进入主互动，未授权或不可发送的频道才安排未关注账号按抖动节奏先关注。',
  },
];

const taskTypes = [
  ['AI 活跃群', '在群内按上下文生成自然对话，适合保持群活跃度。'],
  ['转发监听群', '监听源群消息，经规则过滤、转换和路由后发送到目标群。'],
  ['频道浏览', '为频道消息安排查看动作；已授权可发送频道可直接执行，其他频道先补关注。'],
  ['频道点赞', '为频道消息安排点赞动作；未授权或不可发送时，系统先补关注再执行点赞。'],
  ['频道评论/回复', '在频道讨论区执行评论或回复；需要前置关注时，主互动只使用已关注或刚关注成功的账号。'],
];

const exceptionPlaybook = [
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
        先看任务详情，再看运营数据、归档中心和审计记录，按时间线确认每一步结果。
      </>
    ),
  },
  {
    color: 'cyan',
    children: (
      <>
        <Text strong>频道账号未关注：</Text>
        频道任务启动后会先判断频道能力；已授权且可发送的频道跳过关注前置，未授权或不可发送的频道会生成关注频道前置动作，只补齐未关注账号；全部加入失败时不会进入浏览、点赞或评论主阶段。
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
          defaultActiveKey={['accounts', 'targets', 'tasks']}
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
