# TG 运营管理平台 产品设计文档

## 产品概述

TG 运营管理平台是一个多租户 SaaS 系统，用于统一管理 Telegram 账号、群聊、自动化运营任务和团队协作。平台支持多客户隔离运营，提供从账号接入到消息发送的全流程管理能力。

## 系统架构

- **后端**: Python FastAPI + SQLAlchemy
- **前端**: React + TypeScript + Vite
- **数据库**: PostgreSQL
- **部署**: Docker 容器化

## 角色体系

| 角色 | 权限范围 |
|------|----------|
| 平台管理员 | 管理所有租户、开发者应用、AI 供应商、全局配置 |
| 租户管理员 | 管理本租户账号、群、素材、任务、报表 |
| 操作员 | 创建任务、审核草稿、查看发送结果 |
| 审计员 | 查看审计日志、操作记录 |

## 核心功能模块

### 1. 多租户管理

- 租户创建与配置
- 资源配额管理（账号数、任务数）
- 租户级 AI 设置
- 租户级调度设置

### 2. 账号管理

#### 2.1 账号接入
- 支持手机号 + 验证码登录
- 支持二维码扫码登录
- 登录流程状态追踪
- 2FA 二步验证支持

#### 2.2 账号池管理
- 创建多个账号池，按用途分组
- 账号在池间灵活移动
- 池级联系人管理

#### 2.3 账号健康监控
- 账号状态管理（在线、受限、需重新登录、异常等）
- 健康评分系统
- 定期健康检查

#### 2.4 账号资料同步
- 自动同步 TG 资料（头像、姓名、Bio）
- 资料更新历史记录
- 头像上传与管理

#### 2.5 账号克隆
- 从源账号复制联系人到目标账号
- 从源账号复制群组到目标账号
- 批量克隆任务管理

### 3. 群聊管理

#### 3.1 群同步
- 自动同步 TG 群组信息
- 群成员数、群类型等元数据

#### 3.2 群授权管理
- 已授权运营：可发送消息
- 只读归档：仅可读取历史
- 禁止操作：禁止任何操作

#### 3.3 群策略配置
- 发送时间窗口
- 每日发送上限
- 账号冷却时间
- 群级冷却时间
- 话题方向设定
- 禁词过滤
- 链接白名单
- 审核开关

### 4. 活动任务（Campaign）

#### 4.1 任务创建
- 选择目标群组
- 设定话题和发送强度
- 配置发送时间窗口
- 关联素材库内容

#### 4.2 AI 话术生成
- 自动读取群上下文
- AI 生成多条候选消息
- 支持对话脚本生成
- 多 AI 供应商切换

#### 4.3 草稿审核
- 单条审核（批准/驳回）
- 批量审核
- 风险等级标注

#### 4.4 消息发送
- 智能账号选择
- 自动重试机制
- 发送结果追踪

### 5. 直发消息

- 针对单个账号或账号池直接发送消息
- 支持群发和私聊
- 发送记录查询

### 6. AI 配置

#### 6.1 AI 供应商管理
- 支持 OpenAI 兼容接口
- API Key 加密存储
- 健康状态检测

#### 6.2 Prompt 模板
- 平台级模板
- 租户级自定义模板
- 模板版本管理

#### 6.3 AI 设置
- 默认供应商配置
- 温度、Token 数等参数
- 降级策略（Mock 模式）

### 7. 素材管理

- 文本素材库
- 素材分类与标签
- 使用次数统计

### 8. 群聊归档

#### 8.1 归档内容
- 历史消息归档
- 成员清单归档
- 活跃度分析

#### 8.2 归档导出
- JSON 格式导出
- 成员邀请清单生成

### 9. 验证码管理

- 自动采集 TG 验证码
- 验证码查看记录
- 验证任务处理

### 10. 开发者应用管理

- Telegram API 凭证管理
- API ID/Hash 加密存储
- 应用健康检测
- 账号分配上限

### 11. 调度配置

- 消息发送抖动范围
- 批次间隔设置
- 发送窗口控制

### 12. 审计与报表

#### 12.1 审计日志
- 操作人、操作类型、目标记录
- 时间范围筛选
- IP 地址记录

#### 12.2 运营报表
- 账号状态概览
- 任务执行统计
- 发送成功率分析

## 数据模型

### 核心实体

- **Tenant**: 租户
- **AppUser**: 系统用户
- **TgAccount**: TG 账号
- **AccountPool**: 账号池
- **TgGroup**: TG 群组
- **Campaign**: 活动任务
- **AiDraft**: AI 生成草稿
- **MessageTask**: 消息发送任务
- **Material**: 素材
- **GroupArchive**: 群归档
- **AuditLog**: 审计日志

### 状态流转

#### 账号状态
```
待登录 → 等待验证码/等待扫码 → 等待2FA → 在线
                                      ↓
                              需重新登录/受限/异常/禁用
```

#### 任务状态
```
草稿 → 待审核 → 已审核 → 排队中 → 发送中 → 已发送
                    ↓                    ↓
                  已驳回               失败/已取消
```

## API 接口

### 认证接口
- `POST /api/auth/login` - 用户登录
- `GET /api/auth/me` - 获取当前用户
- `POST /api/auth/logout` - 退出登录

### 账号管理
- `GET /api/tg-accounts` - 账号列表
- `POST /api/tg-accounts` - 创建账号
- `POST /api/tg-accounts/{id}/login/start` - 开始登录
- `POST /api/tg-accounts/{id}/login/verify` - 验证登录
- `POST /api/tg-accounts/{id}/health-check` - 健康检查
- `POST /api/tg-accounts/{id}/sync-groups` - 同步群组

### 群管理
- `GET /api/groups` - 群列表
- `PATCH /api/groups/{id}` - 更新群策略
- `POST /api/groups/{id}/authorize` - 群授权

### 活动任务
- `GET /api/campaigns` - 任务列表
- `POST /api/campaigns` - 创建任务
- `POST /api/campaigns/{id}/generate-drafts` - 生成草稿
- `POST /api/campaigns/{id}/approve-all` - 批量审核

### AI 配置
- `GET /api/ai-providers` - AI 供应商列表
- `GET /api/prompt-templates` - Prompt 模板列表
- `GET /api/tenant-ai-settings` - AI 设置

### 消息任务
- `GET /api/message-tasks` - 任务列表
- `POST /api/message-tasks/{id}/dispatch` - 分发任务
- `POST /api/message-tasks/{id}/retry` - 重试任务

### 归档
- `GET /api/archives` - 归档列表
- `POST /api/archives` - 创建归档
- `POST /api/archives/{id}/export` - 导出归档

### 审计报表
- `GET /api/audit-logs` - 审计日志
- `GET /api/reports` - 运营报表
- `GET /api/overview` - 概览数据

## 安全设计

- JWT Token 认证
- API Key 加密存储
- 多租户数据隔离
- 操作审计追踪
- 敏感信息脱敏

## 部署要求

- Python 3.12+
- Node.js 18+
- PostgreSQL
- Redis（可选，用于任务队列）
