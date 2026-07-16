# TvHeadend Manager

TvHeadend Manager 是一个适合电脑、平板和手机使用的中文 Tvheadend 管理网页。它可以查看节目单、频道、观看记录、客户端、调谐器与信号，也可以安排和管理录像。

界面除节目进度条继续使用 MD3e 样式外，其余页面、按钮、选择框、弹窗和底栏统一使用 Liquid Glass 风格。颜色默认跟随系统，也可以手动选择浅色或深色。

## Docker 安装

需要先安装 Docker 和 Compose 插件。在项目目录运行：

```bash
docker compose up -d --build
```

然后打开 `http://服务器地址:8088`。查看日志：

```bash
docker compose logs -f tvheadend-manager
```

数据库保存在 `tvh-data` 数据卷中，重新构建容器不会删除已有设置和观看记录。

## Debian 直接运行

Debian 12/13 安装 Python 3 后，在项目目录运行：

```bash
TVHMON_DATA_DIR=./data python3 app.py --host 0.0.0.0 --port 8088
```

只允许本机访问时，把地址换成 `127.0.0.1`。同时监听 IPv6 和 IPv4 时使用：

```bash
TVHMON_DATA_DIR=./data python3 app.py --host :: --port 8088
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

外网使用时建议设置网页内登录账号，并让 Caddy 或 Nginx 提供 HTTPS：

```bash
export TVHMON_WEB_USERNAME=admin
export TVHMON_WEB_PASSWORD='请换成长密码'
export TVHMON_TRUSTED_PROXIES='127.0.0.1/32,::1/128'
export TVHMON_COOKIE_SECURE=1
```

在“连接设置”中填写唯一允许的域名并打开“外部访问强制 HTTPS”。不要直接把 8088 或 Tvheadend 的 9981 端口暴露到公网。局域网地址不要求登录；外部地址使用网页自己的登录框，会话只保存在内存中。

需要给播放器提供入口时，可以在同一页面启用安全转发端口。它只放行播放列表、XMLTV、频道流和频道图片，不会转发 Tvheadend 网页、API、Mux、Service 或录像路径。

## 构建 Debian 单文件程序

构建机需要 Python、venv 和网络：

```bash
chmod +x scripts/build-binary.sh
./scripts/build-binary.sh
TVHMON_DATA_DIR=./data ./dist/tvheadend-manager --host 0.0.0.0 --port 8088
```

建议在与目标机器相同或更老的 Debian 版本上构建。

## 数据位置

默认数据库是 `data/tvheadend-manager.db`。它保存连接设置和观看记录，不会被 Git 提交。频道、节目单、信号和录像状态会在启动后重新从 Tvheadend 获取。

项目使用 MIT License。
