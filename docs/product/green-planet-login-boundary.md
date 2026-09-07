# 绿行星平台登录：来源与边界核查

本次仅核查代码与已有证据，未修改 DigitAgent 代码，未进行供应商联调或生产验证；无界面影响。
核查版本：`luaxlou/digitagent` 的 `c9b59cb81c17c899f480c289e4cccee471334487`。

## 区分两个实现

| 项目 | 当前条目指向的旧版 | 仓库中的 Go 版 |
| --- | --- | --- |
| 代码位置 | `apps/customer-web/src/app/api/auth` | `backend/internal/identity` |
| HTTP 入口 | `/api/auth/request-code`、`verify-code`、`logout` | `/api/v1/customer/login/context`、`login/otp/request`、`login/otp/verify`、`session`、`logout` |
| 会话 | `digi_agent_session` | `da_local_customer_session`；与 staff、collaborator 域隔离 |
| 业务耦合 | 校验验证码时还执行客户创建、购买归因和权益开通 | OTP 消费与身份/手机号绑定创建位于 identity；核查的客户 OTP 路径不执行购买归因或权益开通 |
| 验证边界 | 库内是旧版测试记录；本次未复跑 | 仓库记录本地 API、worker、MySQL、Redis 运行证据；外部供应商是协议替身，不代表生产可用 |

不能保留旧接口、旧 Cookie 和旧测试记录，却把代码位置改成 Go 模块。

## 登录能力边界

| 边界 | 内容 |
| --- | --- |
| 认证核心 | 登录上下文、申请与校验验证码、验证身份状态、签发/查询/撤销会话、防重放与限流 |
| 平台策略 | 客户首登是否开户、登录渠道、身份域与 audience、Cookie/Origin、身份绑定与禁用规则 |
| 不属于认证核心 | 商品购买资格、推广归因、支付、权益开通 |
| 实际依赖 | 身份和挑战持久化、通知投递、会话存储、配置与密钥；Go 版还依赖现有 Nova/模块内部代码 |

Go 模块位于 `internal/identity`，不能被其他 Go module 当作公共包直接导入。
它是后续抽象评估的候选来源，不是已经提取完成的通用库。
平台登录服务的接入复用与提取独立认证代码库是两种不同交付。

## 代码与证据

- 旧版 `apps/customer-web/src/app/api/auth/verify-code/route.ts`：`POST` 调用 `resolveAttribution`、`grantCustomerAiCivilizationAccess`、`createSession`。
- Go `backend/internal/identity/http/login.go`：客户/协作者 OTP 与员工企业微信分开。
- Go `backend/internal/identity/verify_otp.go`：消费 OTP、按客户首登策略创建身份绑定、调用 `IssueSessionAtVersion`。
- Go `backend/internal/identity/realm.go`：固定三域的 audience、Cookie 与 Origin。
- `backend/api/openapi.yaml`：Go 版 HTTP 契约。
- `backend/README.md`、`docs/verification/2026-08-31-identity-login.md`：本地运行范围和未完成边界。
- `backend/cmd/api/login_provider.go`：默认构建未配置正式企业微信适配器。

## 待明确的交付

建议从 Go 版客户登录模块继续梳理，但需确认复用的是平台登录服务，还是可接入其他产品的独立认证代码。
在来源和交付方式确定前，不迁移当前条目的代码指针、接口或验证状态，也不声称抽象完成。
