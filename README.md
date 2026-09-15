# QD for Python3

这是一个基于 [qd-today/qd](https://github.com/qd-today/qd) 的兼容增强 Fork。项目继续使用 QD 的 HAR 编辑、变量、断言、日志和定时任务能力，并为需要现代浏览器 TLS 指纹的站点增加按请求启用的传输方式。

上游项目的原 README 已完整归档到本地工作区外层的 `docs/` 目录，该目录不提交到 GitHub。原作者、贡献者及许可证归属不变。

## 本 Fork 的增强

- HAR 请求可通过内部头 `X-QD-Impersonate` 选择 `curl_cffi` 浏览器指纹传输。
- 未设置内部头的请求继续使用 QD 原有 Tornado/PyCurl 链路，现有模板行为不变。
- 内部头只负责 QD 请求路由，发送到目标网站前会被删除。
- 提供适配 NodeSeek 当前 Cloudflare/TLS 环境的 Cookie 签到 HAR。
- GitHub Actions 仅构建 `linux/amd64` 镜像并发布到 GitHub Container Registry。

NodeSeek HAR 浏览器指纹传输设计保存在本地工作区外层的 `docs/` 目录，不提交到 GitHub。

## 快速部署

固定版本部署：

```bash
docker pull ghcr.io/ymting/qd:20260716.2
docker run -d \
  --name qd \
  --restart unless-stopped \
  -p 8923:80 \
  -v "$PWD/config:/usr/src/app/config" \
  ghcr.io/ymting/qd:20260716.2
```

需要自动跟随最新正式版本时，将镜像改为：

```text
ghcr.io/ymting/qd:latest
```

浏览器访问 `http://服务器地址:8923`。生产部署前请按 QD 原有配置方式设置安全的 `COOKIE_SECRET`、`AES_KEY` 和数据库参数，不要沿用公开示例密钥。

当前 Fork 镜像只支持 64 位 x86，即 Docker 平台 `linux/amd64`。

仓库自带的 Compose 配置默认使用固定版本 `20260716.2`：

```bash
docker compose up -d
```

需要切换到 `latest` 或指定回滚版本时，可覆盖镜像变量：

```bash
QD_IMAGE=ghcr.io/ymting/qd:latest docker compose up -d
```

## 烧饼论坛签到（可选浏览器助手）

模板：[烧饼论坛-签到.har](templates/烧饼论坛-签到.har)。导入模板后新建任务，按需选择 Cookie 或账号密码模式。模板变量名均为 ASCII，避免 QD 前端变量解析和保存异常。

### Cookie 模式

Cookie 模式不需要启动浏览器助手：

1. 在浏览器中登录烧饼论坛并正常完成人机验证。
2. 将 HAR 导入 QD，并将 `login_mode=cookie`（留空也会使用 Cookie 模式）。
3. 将浏览器当前会话的完整 Cookie 填入 `cookie`；`username`、`password` 和 `profile_name` 留空。
4. 先手动执行一次任务，确认签到结果后再启用定时运行。

Cookie 失效时应在浏览器重新登录并更新任务变量，不要把真实 Cookie 写入 HAR、README、日志或 Git 文件。

### 账号密码模式

账号密码模式通过可选的 `sb-forum-browser` 服务复用独立 Chromium Profile。首次启用前先生成至少 24 个字符的随机 token，并将同一个值同时配置到宿主机环境或 QD 目录下的 `.env` 文件，以及任务变量 `browser_api_token`：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```dotenv
SB_FORUM_API_TOKEN=请替换为上一步生成的随机值
```

未设置 `SB_FORUM_API_TOKEN` 时，浏览器服务应拒绝启动；Cookie 模式无需设置 token。token 只用于 QD 与浏览器助手之间的内部认证，不得提交到仓库、写入 README 或输出到日志。

账号密码模式还必须运行包含本次 `libs/fetcher.py` / `X-QD-Direct` 支持的 QD Fork 构建或镜像。仓库当前 Compose 默认的旧固定镜像不含该改动，相关镜像发布前不能将它描述为已支持密码模式；Cookie 模式不受此内部直连要求影响。准备好对应镜像后，再启动 profile（token 必须已经配置）：

```bash
QD_IMAGE=your-qd-fork-image:tag docker compose --profile sb-forum up -d --build
```

该 profile 默认不会启动；不使用账号密码模式时，普通的 `docker compose up -d` 不会构建或启动浏览器助手。账号密码模式的任务变量如下：

- `browser_api_token`：与 `SB_FORUM_API_TOKEN` 相同的共享密钥
- `login_mode=password`
- `username`：烧饼论坛用户名
- `password`：烧饼论坛密码
- `profile_name`：独立 Profile 名称，只使用字母、数字、连字符或下划线

密码模式下 `cookie` 可以留空。首次执行会打开论坛登录页并填入账号密码，但不破解或伪造 Cap CAPTCHA。请在本机打开 `http://127.0.0.1:6081/vnc.html`，按页面提示人工完成验证码并提交登录；远程服务器请先通过 SSH 隧道转发该端口。首次交互可能超过 QD 默认的 30 秒请求超时，但浏览器助手仍会在最多 90 秒内等待；完成验证码后请手动重试任务，或等待 QD 的下一次重试。登录成功后，服务会在 `./sb-forum-browser-data` 中持久化对应 Profile，后续任务复用登录态。若登录态失效或再次要求验证码，请重新打开 noVNC 完成人工操作。

浏览器助手的 `SB_FORUM_PORT=8766` 只在 Compose 内部网络提供，不映射到宿主机；noVNC 只绑定宿主机回环地址 `127.0.0.1:6081`，不要把它改为公网监听。

### 签到日期与安全提示

烧饼论坛按 UTC 计算签到日期，因此北京时间（UTC+8）每天 **08:00** 后才进入新的签到日。请据此安排 QD 的定时任务，重复执行当天签到会按站点结果作为幂等成功处理。

浏览器 Profile、Cookie、密码、CSRF 和验证码相关数据都属于敏感信息。运行目录已加入 Git 与 Docker 构建忽略规则，但忽略规则不能清除已经被提交或备份的文件；请限制 `sb-forum-browser-data` 的文件权限，不要提交、上传或在日志中打印这些内容。论坛验证码必须由用户在真实页面中完成，项目不会提供绕过或破解验证码的功能。

## 镜像标签

| 标签 | 用途 |
| --- | --- |
| `ghcr.io/ymting/qd:20260716.2` | 本次正式发布版本，推荐生产环境固定使用 |
| `ghcr.io/ymting/qd:latest` | 最新正式版本，发布新版本时更新 |
| `ghcr.io/ymting/qd:sha-<提交短哈希>` | 精确对应源码提交，便于定位和回滚 |

三个标签由同一次构建生成并引用同一镜像，不会重复构建镜像层。

## 模板版本管理

QD 数据库保存的是**导入当时**的 HAR 快照，仓库里的模板更新后不会自动同步到已有任务。
为了能回答「这个任务到底跑的是哪一版模板」，本仓库做了三件事：

1. 每个模板的签到日志末尾都会输出 `（模板 v20260915.1）` 形式的版本号。
   推送消息里**看不到版本号**，就说明该任务还在运行旧模板，需要重新导入。
2. `templates/manifest.json` 登记每个模板的版本号、SHA-256、条目数、必需变量和外层镜像状态。
3. `tools/template_manifest.py` 负责生成与校验，`tests/test_template_manifest.py` 接入测试。

改动模板后按下面的顺序操作：

```bash
# 1. 修改 templates/<模板>.har，并在 templates/CHANGELOG.md 记录行为变化
# 2. 升级版本号（同时更新 updated 日期）
python tools/template_manifest.py --set-version 吾爱破解-签到.har 20260915.2
# 3. 把模板里的日志版本号同步改成 v20260915.2，然后重算哈希与条目数
python tools/template_manifest.py --write
# 4. 校验清单与模板一致，并跑测试
python tools/template_manifest.py --check
python -m pytest tests -q
```

清单里的 `mirror` 字段用于记录外层工作区 `templates/` 中的副本，`state` 为 `synced`
时校验器会要求逐字节一致；仓库单独克隆看不到该目录时会自动跳过。

## NodeSeek 签到

模板：[NodeSeek-可选签到模式.har](templates/NodeSeek-可选签到模式.har)

1. 在浏览器中登录 NodeSeek，并完成人机验证。
2. 将 HAR 导入 QD，新建对应任务。
3. 将任务变量 `cookie` 设置为浏览器中复制的完整 Cookie。
4. 设置任务变量 `sign_mode`；只填写下表中的小写英文值。
5. 手动执行一次任务，确认 Cookie、网络出口和签到结果正常后再启用定时运行。

| 配置值 | 签到方式 |
| --- | --- |
| `fixed` | 固定获得 5 积分，也是留空时的默认模式 |
| `random` | 使用随机积分模式 |

不要填写布尔值 `true` 或 `false`。模板会自行把 `fixed` 和 `random` 转换为 NodeSeek 接口需要的参数。

模板还提供两个浏览器指纹变量：

- `browser_fingerprint`：留空或填 `chrome`，使用当前镜像中 `curl_cffi` 的最新 Chrome 指纹；也可填 `chrome136`、`chrome142`、`chrome145` 或 `chrome146`。
- `browser_user_agent`：留空或填 `auto`，由 `curl_cffi` 生成与指纹成套的 UA 和 Client Hints；只有确需匹配浏览器现有 `cf_clearance` 时，才填写该浏览器的完整 UA。

从 `20260716.2` 开始，以上变量名全部使用纯英文。QD 的 HAR 前端变量提取器不支持变量名中包含中文；旧变量名会被截断，导致提交后值被置空并回退到固定模式。升级后必须重新导入本仓库中的 HAR，再新建任务或重新填写变量。

模板中的 `X-QD-Impersonate` 是本 Fork 使用的内部路由标记，发送到 NodeSeek 前会被删除。重复签到返回 HTTP 500 和“今天已完成签到”时，模板会将其识别为正常完成。

### NodeSeek 403 排障

`HTTP 403` 表示 TLS 请求已经成功到达网站，但 Cloudflare 或 NodeSeek 拒绝了当前请求。即使浏览器和容器的公网 IP、UA 完全相同，浏览器重新验证后自动更新的 `cf_clearance` 也不会同步到 QD；隔夜 Cookie、修改密码后的旧 Cookie、过期的 `session`/`pjwt` 都可能导致 403。

按以下顺序处理：

1. 在同一浏览器配置文件中重新打开 NodeSeek，确认已登录并完成人机验证。
2. 在开发者工具的网络面板中确认 `/api/account/credit/page-1` 返回 200。
3. 立即复制该请求当前的完整 Cookie，整体覆盖 QD 任务变量 `cookie`，不要只替换其中一个字段。
4. 保持指纹和 UA 为默认 `chrome` + `auto`，立即手动运行一次 QD 任务。

从 `20260716.1` 开始，失败日志会增加脱敏的 `Response Diagnostic`：

- `Cloudflare challenge/WAF`：重新完成人机验证并更新完整 Cookie。
- `Cloudflare edge rejection`：更新 `cf_clearance`，并检查浏览器指纹与 UA 是否成套。
- `NodeSeek application/auth`：NodeSeek 登录态失效，重新登录后更新完整 Cookie。

诊断日志不会输出请求 Cookie 或认证令牌。不要删除模板对 401/403/429 的失败断言，也不要把 403 当作签到成功。

## Cookie 与验证边界

- 本项目不会使用账号密码登录 NodeSeek，也不会绕过 Cloudflare 人机验证。
- Cookie、`pjwt`、`cf_clearance` 等内容仅应保存为 QD 的私有任务变量。
- 不要把真实 Cookie 写入 HAR、README、Issue、Actions 日志或其他 Git 文件。
- Cookie 失效或 Cloudflare 要求重新验证时，需要在浏览器重新登录并更新任务变量。
- 浏览器指纹传输只能解决客户端 TLS 兼容问题，不能保证任意数据中心 IP 都能通过站点风控。

## 文档

- 本地维护文档统一保存在工作区外层的 `docs/` 目录，不提交到 GitHub。
- 项目记忆统一保存在工作区外层的 `memory/` 目录，不提交到 GitHub。
- [更新日志](CHANGELOG.md)
- [MIT 许可证](LICENSE)

升级前请查看 [CHANGELOG.md](CHANGELOG.md)。出现回归时，可将镜像从 `latest` 固定到版本标签或对应的 `sha-<提交短哈希>`。

## 致谢与许可

感谢 [qd-today/qd](https://github.com/qd-today/qd)、其原作者和所有贡献者。本 Fork 仅维护上述兼容增强；QD 的完整原始介绍和贡献者名单已归档到本地工作区外层的 `docs/` 目录。

本项目继续遵循 [MIT License](LICENSE)。
