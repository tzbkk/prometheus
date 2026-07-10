# Prometheus - QQ 频道帖子归档

通过 AppImage 解包 + 代码注入，Hook `JSON.parse` 捕获完整 feed 数据（80+ 字段/条），下载媒体文件，捕获滚动时 QQ 预加载的评论。

**已实现**: 帖子 + 评论 + 图片/视频全部覆盖，跨轮幂等去重，守护模式自动刷新。

## 先决条件

| 条件 | 说明 |
|------|------|
| Linux + Wayland | QQ Electron 仅在 Wayland 下可靠运行（`--ozone-platform=wayland`） |
| QQ AppImage (Linux) | 从 [im.qq.com/linuxqq](https://im.qq.com/linuxqq/index.shtml) 下载 AppImage（已验证 v3.2.29）|
| 正常 QQ 不能同时运行 | patched QQ 与正常 QQ 共享 `~/.config/QQ/` → 版本冲突崩溃 |
| Python ≥ 3.8 | `start_qq.sh` 通过 `_envconfig.py` 把 JSON 配置转环境变量 |
| 首次 QQ 登录 | 启动后需手机扫码登录；session 持久化在 `~/.config/QQ/`，后续重启免登录 |

## 快速使用

```bash
# 0. 克隆/复制本项目到 ~/Projects/prometheus

# 1. 一键安装（从 AppImage 构建 patched QQ，自动伪装版本避免 hotUpdate 崩溃）
bash scripts/setup.sh ~/Downloads/QQ_3.2.29_260528_x86_64_01.AppImage

# 2. 启动（会检查前置条件、防重复启动、打印监控路径）
bash scripts/start_qq.sh

# 3. 首次启动需手机扫码登录 QQ（仅一次）
#    后续重启自动恢复 session

# 4. 监控进度
tail -f data/prometheus.log
# 关键日志：
#   "Clicking: <频道名>"     — 已自动进入目标频道
#   "ScrollJS N feeds:X"     — 滚动抓帖子中（X 为已抓数）
#   "BOTTOM DETECTED"        — 时间线到底
#   "Daemon: N new posts"    — 守护模式发现新帖/补抓评论

# 5. 守护模式（默认开启）
# 启动后 ~2.5 分钟自动开始，每 5 分钟一轮：滚到顶 → 扫新帖 → 滚到底 → 补评论
```

## 停止

```bash
kill $(pgrep -f "[q]q_patched/qq --ozone")
```

## 文档

| 文档 | 内容 |
|------|------|
| [doc/CONFIGURATION.md](doc/CONFIGURATION.md) | 配置文件详解、环境变量、路径配置、核心参数 |
| [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) | 注入流程、帖子/评论捕获原理、滚动机制、守护模式、限制 |
| [doc/DATA_FORMAT.md](doc/DATA_FORMAT.md) | 数据文件格式、字段说明、常用查询 |
| [doc/DEPLOYMENT.md](doc/DEPLOYMENT.md) | 目录结构、新机器安装、systemd 部署 |
