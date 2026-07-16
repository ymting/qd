# NodeSeek HAR 浏览器指纹传输设计

## 项目背景

NodeSeek 的签到仍适合用 QD HAR 模板表达：模板负责 Cookie 变量、固定或随机签到模式、请求断言和日志提取。当前失败点不在 HAR，而在 QD 的底层请求传输。普通 PyCurl 请求被 Cloudflare 拒绝，现有 JA3 镜像又在 TLS 握手阶段返回 `HTTP 599 quictls SSL_connect: SSL_ERROR_SYSCALL`。

实测 `curl_cffi` 使用 Chrome 110 指纹和匹配的浏览器 User-Agent 可以访问 NodeSeek。因此本设计为 QD 增加按请求启用的浏览器指纹传输，同时保留原有 HAR 执行流程。

## 目标

1. NodeSeek 继续使用标准 HAR 模板，不引入独立签到服务。
2. 只有明确标记的请求使用 `curl_cffi`，其他模板行为保持不变。
3. `curl_cffi` 响应继续进入 QD 原有的 Cookie、变量、断言和日志处理链路。
4. GitHub Actions 为 `linux/amd64` 构建并发布 GHCR 镜像。
5. 首次发布版本为 `20260715.1`，同一镜像同时带版本、`latest` 和提交哈希标签。
6. 真实 Cookie、`pjwt`、`cf_clearance` 等认证信息不进入代码、模板、日志、测试或 Release。

## 非目标

- 不替换 QD 的全局 HTTP 客户端。
- 不实现 NodeSeek 账号密码登录或 Cloudflare 人机验证。
- 不承诺 32 位 x86、32 位 ARM 或其他未提供 `curl_cffi` 预编译包的平台。
- 不修改其他站点 HAR 的默认行为。

## 方案比较

### 方案 A：HAR 按请求选择传输（采用）

HAR 请求增加 QD 内部请求头 `X-QD-Impersonate: chrome110`。QD 解析后删除该请求头，并把请求交给 `curl_cffi`。该方式改动范围有限，NodeSeek 可以获得浏览器 TLS 指纹，其他模板仍使用原传输。

### 方案 B：全局替换为 `curl_cffi`

实现入口更少，但会改变所有模板的重定向、代理、Cookie 和异常行为，回归风险过大。

### 方案 C：独立 NodeSeek 脚本或服务

可以绕过 QD 请求层，但会脱离 HAR 的变量、日志和任务管理，不符合本项目目标。

## 架构设计

### 请求路由

`Fetcher.build_request()` 继续构造 Tornado `HTTPRequest`。请求执行前检查内部头 `X-QD-Impersonate`：

- 未设置：沿用 `AsyncHTTPClient.fetch()`。
- 值为 `chrome110`：删除内部头，调用新的 `curl_cffi` 适配器。
- 值不受支持：返回明确的配置错误，不回退到普通传输。

内部头只用于 QD 路由，不发送到目标网站，也不作为响应日志中的站点请求头展示。

### 适配器边界

新增 `libs/curl_cffi_client.py`，职责限定为：

1. 将 Tornado `HTTPRequest` 映射为 `curl_cffi.requests.AsyncSession` 请求。
2. 传递方法、URL、请求头、请求体、连接超时、总超时和代理配置。
3. 保持 QD 当前的不自动重定向和证书校验策略。
4. 执行由 `X-QD-Impersonate` 指定的 Chrome 浏览器指纹模拟；NodeSeek 模板默认使用 `chrome` 别名。
5. 将响应状态、原因、响应头和响应体转换为 Tornado `HTTPResponse`。
6. 执行下载大小限制，避免浏览器指纹传输绕过 QD 的资源约束。

适配器返回 Tornado `HTTPResponse` 后，后续仍由现有 `Fetcher` 负责 Cookie 合并、编码判断、HAR 响应生成、断言和变量提取。

### NodeSeek HAR

NodeSeek 模板的签到请求和积分记录请求都添加内部指纹头。模板继续提供任务变量：

- `fixed`：请求 `POST /api/attendance?random=false`，固定获得 5 积分。
- `random`：请求 `POST /api/attendance?random=true`，获得随机积分。
- 留空：默认使用 `fixed`。

重复签到返回 HTTP 500 和“今天已完成签到”时，仍由 HAR 断言将其识别为正常完成。

## 错误处理

- 未安装 `curl_cffi`：只有启用指纹传输的请求失败，并提示镜像或依赖缺失；QD 服务本身仍可启动。
- 指纹值不受支持：返回包含该值的配置错误，避免静默使用错误 TLS 指纹。
- TLS、连接或超时异常：转换为 QD 可记录的 HTTP 599 响应，保留可诊断的异常信息。
- 响应超过下载限制：终止读取并返回明确的大小限制错误。
- NodeSeek Cookie 失效：由模板登录校验或接口响应断言报告，不尝试自动登录或绕过验证。
- 401、403、429 响应：只根据 Server、Content-Type、cf-mitigated 和正文标记生成脱敏分类，不记录请求 Cookie、Authorization 或完整响应正文。

核心路由和异常转换处添加中文注释，说明内部请求头的安全边界以及响应适配原因。

## Docker 与 GitHub 发布

新增 fork 专用 Dockerfile，直接复制 GitHub Actions 检出的本仓库代码。不能沿用当前 Dockerfile 在构建阶段从 Gitee 克隆上游的做法，否则 fork 修改会被覆盖。

新增 GHCR 发布工作流：

- 触发条件：GitHub Release 发布，也支持手动触发用于验证。
- 构建平台：仅 `linux/amd64`。
- 发布地址：`ghcr.io/ymting/qd`。
- 权限：使用仓库自带 `GITHUB_TOKEN` 的 `packages: write`，不需要 Docker Hub 密钥。
- 构建策略：一次 Buildx 构建和一次镜像层上传，同时写入多个标签。

版本 `20260715.1` 发布后的标签为：

```text
ghcr.io/ymting/qd:20260715.1
ghcr.io/ymting/qd:latest
ghcr.io/ymting/qd:sha-<提交短哈希>
```

三个标签引用同一镜像清单，不重复构建，也不会把镜像层存储扩大三倍。

## README 与上游归档

当前根 README 的徽章、工作流、镜像地址和大部分链接都指向 `qd-today/qd`，继续作为 fork 首页会让使用者误以为构建状态和镜像属于本仓库。

发布前执行以下整理：

1. 将当前上游 README 完整归档为 `docs/archive/upstream-readme-20260715.md`，并生成同目录 HTML 阅读版。
2. 新的根 README 明确说明本项目基于 `qd-today/qd`，提供上游仓库和归档 README 的超链接。
3. 首页只展示 `ymting/qd` 的真实状态、GHCR 镜像地址、`linux/amd64` 约束、NodeSeek HAR 使用方式和安全注意事项。
4. 删除或替换指向上游构建状态、Docker Hub 镜像和仓库统计的误导徽章。
5. 保留 MIT 许可证、原作者和贡献者归属；完整贡献者名单继续保存在归档 README 中。
6. 根 README 更新后生成 `docs/README.html`，满足项目的 Markdown/HTML 文档流水线要求。

新的根 README 至少包含：项目定位、与上游的关系、本次增强、镜像拉取与启动示例、镜像标签说明、NodeSeek HAR 配置、Cookie 安全提示、文档链接、更新与回滚、许可证和致谢。

## 测试与验收

### 自动测试

1. 无内部指纹头时仍调用原 Tornado/PyCurl 客户端。
2. `chrome`、`chrome110` 和受支持的较新 Chrome 标记正确路由到 `curl_cffi`。
3. 内部请求头在发送前被删除。
4. 方法、URL、请求头、请求体、超时和代理映射正确。
5. `curl_cffi` 响应正确转换为 Tornado `HTTPResponse`。
6. 缺少依赖、未知指纹、TLS 异常和下载超限均产生明确错误。
7. 现有 HAR 响应、Cookie 和断言处理继续工作。
8. README、归档 README 和 NodeSeek HAR 中不存在真实 Cookie 或认证令牌。

### 端到端验收

1. 使用环境变量临时注入有效 NodeSeek Cookie，不写入文件。
2. 在修改后的 QD Fetcher 中执行 NodeSeek HAR。
3. 验证固定模式请求 `random=false`，随机模式请求 `random=true`。
4. 验证首次签到成功和当天重复签到都得到正确任务结果。
5. 创建 GitHub Release `20260715.1`。
6. 等待 GitHub Actions 完成，确认三个 GHCR 标签均指向同一可拉取的 `linux/amd64` 镜像。
7. 检查 GitHub 项目首页的 README 链接、GHCR 地址和上游归档链接均可用。

## 发布与回滚

发布前更新 `version.json` 和 `CHANGELOG.md`。Release 说明使用中文，概括 NodeSeek HAR 浏览器指纹支持、镜像架构和使用方式。

如果 `latest` 出现回归，可立即把部署镜像固定到 `20260715.1` 或对应 `sha-<提交短哈希>`；后续修复发布新版本后再移动 `latest`。
