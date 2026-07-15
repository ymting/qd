# QD for Python3

这是一个基于 [qd-today/qd](https://github.com/qd-today/qd) 的兼容增强 Fork。项目继续使用 QD 的 HAR 编辑、变量、断言、日志和定时任务能力，并为需要现代浏览器 TLS 指纹的站点增加按请求启用的传输方式。

上游项目的原 README 已完整保存在 [Markdown 归档](docs/archive/upstream-readme-20260715.md) 和 [HTML 阅读版](docs/archive/upstream-readme-20260715.html)。原作者、贡献者及许可证归属不变。

## 本 Fork 的增强

- HAR 请求可通过内部头 `X-QD-Impersonate: chrome110` 选择 `curl_cffi` 浏览器指纹传输。
- 未设置内部头的请求继续使用 QD 原有 Tornado/PyCurl 链路，现有模板行为不变。
- 内部头只负责 QD 请求路由，发送到目标网站前会被删除。
- 提供适配 NodeSeek 当前 Cloudflare/TLS 环境的 Cookie 签到 HAR。
- GitHub Actions 仅构建 `linux/amd64` 镜像并发布到 GitHub Container Registry。

详细设计见 [NodeSeek HAR 浏览器指纹传输设计](docs/superpowers/specs/2026-07-15-nodeseek-curl-cffi-transport-design.md)。

## 快速部署

固定版本部署：

```bash
docker pull ghcr.io/ymting/qd:20260715.1
docker run -d \
  --name qd \
  --restart unless-stopped \
  -p 8923:80 \
  -v "$PWD/config:/usr/src/app/config" \
  ghcr.io/ymting/qd:20260715.1
```

需要自动跟随最新正式版本时，将镜像改为：

```text
ghcr.io/ymting/qd:latest
```

浏览器访问 `http://服务器地址:8923`。生产部署前请按 QD 原有配置方式设置安全的 `COOKIE_SECRET`、`AES_KEY` 和数据库参数，不要沿用公开示例密钥。

当前 Fork 镜像只支持 64 位 x86，即 Docker 平台 `linux/amd64`。

仓库自带的 Compose 配置默认使用固定版本 `20260715.1`：

```bash
docker compose up -d
```

需要切换到 `latest` 或指定回滚版本时，可覆盖镜像变量：

```bash
QD_IMAGE=ghcr.io/ymting/qd:latest docker compose up -d
```

## 镜像标签

| 标签 | 用途 |
| --- | --- |
| `ghcr.io/ymting/qd:20260715.1` | 本次正式发布版本，推荐生产环境固定使用 |
| `ghcr.io/ymting/qd:latest` | 最新正式版本，发布新版本时更新 |
| `ghcr.io/ymting/qd:sha-<提交短哈希>` | 精确对应源码提交，便于定位和回滚 |

三个标签由同一次构建生成并引用同一镜像，不会重复构建镜像层。

## NodeSeek 签到

模板：[NodeSeek-可选签到模式.har](templates/NodeSeek-可选签到模式.har)

1. 在浏览器中登录 NodeSeek，并完成人机验证。
2. 将 HAR 导入 QD，新建对应任务。
3. 将任务变量 `cookie` 设置为浏览器中复制的完整 Cookie。
4. 设置任务变量 `sign_mode_填random随机_填fixed固定5`；只填写下表中的小写英文值。
5. 手动执行一次任务，确认 Cookie、网络出口和签到结果正常后再启用定时运行。

| 配置值 | 签到方式 |
| --- | --- |
| `fixed` | 固定获得 5 积分，也是留空时的默认模式 |
| `random` | 使用随机积分模式 |

不要填写布尔值 `true` 或 `false`。模板会自行把 `fixed` 和 `random` 转换为 NodeSeek 接口需要的参数。

模板中的 `X-QD-Impersonate: chrome110` 是本 Fork 使用的内部路由标记，已经配置完成，用户无需修改。重复签到返回 HTTP 500 和“今天已完成签到”时，模板会将其识别为正常完成。

## Cookie 与验证边界

- 本项目不会使用账号密码登录 NodeSeek，也不会绕过 Cloudflare 人机验证。
- Cookie、`pjwt`、`cf_clearance` 等内容仅应保存为 QD 的私有任务变量。
- 不要把真实 Cookie 写入 HAR、README、Issue、Actions 日志或其他 Git 文件。
- Cookie 失效或 Cloudflare 要求重新验证时，需要在浏览器重新登录并更新任务变量。
- 浏览器指纹传输只能解决客户端 TLS 兼容问题，不能保证任意数据中心 IP 都能通过站点风控。

## 文档

- [上游 README 归档](docs/archive/upstream-readme-20260715.md)
- [项目 README HTML 阅读版](docs/README.html)
- [浏览器指纹传输设计](docs/superpowers/specs/2026-07-15-nodeseek-curl-cffi-transport-design.md)
- [更新日志](CHANGELOG.md)
- [MIT 许可证](LICENSE)

升级前请查看 [CHANGELOG.md](CHANGELOG.md)。出现回归时，可将镜像从 `latest` 固定到版本标签或对应的 `sha-<提交短哈希>`。

## 致谢与许可

感谢 [qd-today/qd](https://github.com/qd-today/qd)、其原作者和所有贡献者。本 Fork 仅维护上述兼容增强；QD 的完整原始介绍和贡献者名单保存在[上游 README 归档](docs/archive/upstream-readme-20260715.md)。

本项目继续遵循 [MIT License](LICENSE)。
