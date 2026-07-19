# TvHeadend Manager

TvHeadend Manager 是一个适合电脑、平板和手机使用的中文 Tvheadend 管理网页。它可以查看节目单、频道、观看记录、客户端、调谐器与信号，也可以安排和管理录像。

界面除节目进度条继续使用 MD3e 样式外，其余页面、按钮、选择框、弹窗和底栏统一使用 Liquid Glass 风格。颜色默认跟随系统，也可以手动选择浅色或深色。

## Docker 安装

需要先安装 Docker 和 Compose 插件。在项目目录运行：

```bash
mkdir -p data
docker compose pull
docker compose up -d
```

然后打开：

```text
http://服务器地址:8088
```

查看运行日志：

```bash
docker compose logs -f tvheadend-manager
```

更新镜像：

```bash
docker compose pull
docker compose up -d
```

数据库保存在项目目录的 `./data` 中，更新或重建容器不会删除已有设置和观看记录。迁移时停止容器并复制整个 `data` 目录即可。

Compose 默认以宿主机的 `1000:1000` 用户运行。当前账号不是这个 UID/GID 时，使用下面的方式启动或更新：

```bash
PUID=$(id -u) PGID=$(id -g) docker compose up -d
```

默认使用 `ghcr.io/hiper25/tvheadend-client-web:latest`，同时提供 `amd64` 和 `arm64` 镜像。也可以通过 `TVHMON_IMAGE` 换成指定版本，例如：

```bash
TVHMON_IMAGE=ghcr.io/hiper25/tvheadend-client-web:1.2.2 docker compose up -d
```

GitHub 第一次产出镜像后，仓库所有者需要在对应 Package 的设置中把可见性改为 Public。公开前只有登录过 GHCR 的设备可以拉取镜像。

## Debian 单文件安装

打开项目的 GitHub Releases，下载同一版本的 `tvheadend-manager-版本-debian12-amd64` 和 `.sha256` 文件。校验并赋予执行权限：

```bash
sha256sum -c tvheadend-manager-*-debian12-amd64.sha256
chmod +x tvheadend-manager-*-debian12-amd64
```

运行程序：

```bash
TVHMON_DATA_DIR=./data ./tvheadend-manager-*-debian12-amd64 --host 0.0.0.0 --port 8088
```

单文件程序在 Debian 12 环境构建，适用于 `amd64` Debian 12/13。

## Debian 使用 Python 运行

Debian 12/13 安装 Python 3 后，在项目目录运行：

```bash
TVHMON_DATA_DIR=./data python3 app.py --host 0.0.0.0 --port 8088
```

只允许本机访问时，把地址换成 `127.0.0.1`：

```bash
TVHMON_DATA_DIR=./data python3 app.py --host 127.0.0.1 --port 8088
```

同时监听 IPv6 和 IPv4：

```bash
TVHMON_DATA_DIR=./data python3 app.py --host :: --port 8088
```

IPv6 地址放进浏览器时需要使用方括号，例如：

```text
http://[2001:db8::10]:8088
```

## 第一次连接

第一次打开网页会显示连接设置。分别填写 Tvheadend 地址、用户名和密码，例如：

```text
地址：http://192.168.1.10:9981
用户名：tvh-manager
密码：你的密码
```

地址中不要包含用户名和密码。Tvheadend 账号建议开启 Web interface、Video recorder 和 Admin 权限，并把运行 TvHeadend Manager 的设备地址加入允许的网络。

## 外网和 HTTPS

外网使用时建议先设置网页登录账号：

```bash
export TVHMON_WEB_USERNAME=admin
export TVHMON_WEB_PASSWORD='请换成长密码'
export TVHMON_TRUSTED_PROXIES='127.0.0.1/32,::1/128'
export TVHMON_COOKIE_SECURE=1
```

然后让 Caddy 或 Nginx 提供 HTTPS。Caddy 示例：

```caddyfile
tv.example.com {
    reverse_proxy 127.0.0.1:8088
}
```

在“连接设置”中把允许域名填写为 `tv.example.com`，并打开“外部访问强制 HTTPS”。不要直接把 8088 或 Tvheadend 的 9981 端口暴露到公网。

通过 HTTPS 域名打开后，可以把网页安装成 PWA：

- iPhone 和 iPad：Safari 分享菜单 → 添加到主屏幕。
- Android：Chrome 或 Edge 菜单 → 安装应用。
- 电脑：Chrome 或 Edge 地址栏 → 安装。

普通局域网 HTTP 地址仍可使用网页，但不会完整启用 PWA。`localhost` 可用于本机安装测试。

## 常用环境变量

| 变量 | 用途 |
| --- | --- |
| `TVHMON_DATA_DIR` | 数据库保存目录，默认 `./data` |
| `TVHMON_TVH_PASSWORD` | 使用环境变量提供 Tvheadend 密码 |
| `TVHMON_WEB_USERNAME` | 外网网页登录用户名 |
| `TVHMON_WEB_PASSWORD` | 外网网页登录密码 |
| `TVHMON_TRUSTED_PROXIES` | 可信反向代理地址 |
| `TVHMON_COOKIE_SECURE` | HTTPS 下设为 `1` |
| `TVHMON_ALLOWED_HOST` | 唯一允许的外网域名 |
| `TVHMON_REQUIRE_HTTPS` | 设为 `1` 时拒绝外部 HTTP |
| `TVHMON_FORWARD_PORT` | Tvheadend 只读媒体转发端口，`0` 表示关闭 |

## 本地构建 Debian 单文件程序

构建机需要 Python、venv 和网络：

```bash
chmod +x scripts/build-binary.sh
./scripts/build-binary.sh
```

完成后运行：

```bash
TVHMON_DATA_DIR=./data ./dist/tvheadend-manager --host 0.0.0.0 --port 8088
```

建议在与目标机器相同或更老的 Debian 版本上构建。

## 数据位置

默认数据库是 `data/tvheadend-manager.db`。它保存连接设置和观看记录，不会被 Git 提交。频道、节目单、信号和录像状态会在启动后重新从 Tvheadend 获取。

项目使用 MIT License。
