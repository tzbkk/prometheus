# 部署

## 目录结构

```
prometheus/
├── prometheus.conf.json          # 配置（单一真相源）
├── pyproject.toml                # src layout: packages.find where=src
├── requirements.txt
├── README.md
├── doc/                          # 文档（本目录）
├── scripts/                      # 可执行脚本
│   ├── setup.sh                  # 一键安装（解包 AppImage + 注入）
│   └── start_qq.sh               # 启动 patched QQ
├── deploy/                       # 部署配置
│   └── qq-prometheus.service     # systemd user service 模板（默认禁用）
├── src/
│   └── prometheus/               # Python 包（src layout）
│       ├── __init__.py
│       ├── __main__.py           # python -m prometheus 入口
│       ├── config.py             # 配置加载器
│       ├── _envconfig.py         # bash 环境变量发射器
│       ├── inject.js             # 注入脚本（核心，读 env，被复制进 AppImage）
│       ├── autoscroll.py         # ydotool 自动滚动（已弃用）
│       ├── keyring.py            # SQLCipher 密钥提取
│       ├── cipher.py             # SQLCipher AES-256-CBC 解密
│       ├── feed.py               # Protobuf feed 解析
│       ├── media.py              # 本地媒体管理
│       ├── cli.py                # CLI 接口
│       └── scraper.py            # CDP 刮取器（QQ 禁用 CDP，不可用）
├── data/                         # 运行时数据（运行后生成，gitignored）
│   ├── feeds.jsonl               # 帖子归档
│   ├── comments.jsonl            # 评论归档
│   ├── media_index.jsonl         # 媒体索引
│   ├── dead_media.jsonl          # 临时失效媒体队列（重试中）
│   ├── dead_media_permanent.jsonl # 永久失效 URL（3 次重试后放弃）
│   ├── state.json                # 守护状态快照
│   ├── media/                    # 下载的图片/视频
│   ├── ids.json                  # 帖子 ID 去重表
│   └── prometheus.log            # 运行日志
├── qq_patched/                   # AppImage 解包结果（setup.sh 后生成，gitignored）
└── output/                       # CLI 归档产出（archive.db / markdown / media，gitignored）
```

## 新机器安装

```bash
# 1. 复制整个项目目录
scp -r prometheus/ newhost:~/Projects/

# 2. 下载 QQ AppImage
#    https://im.qq.com/linuxqq/index.shtml

# 3. 运行安装脚本（patched QQ 会解包到项目内 qq_patched/）
#    setup.sh 保留 AppImage 原始版本号，仅修改 package.json 入口指向注入脚本。
cd ~/Projects/prometheus
bash scripts/setup.sh ~/Downloads/QQ_3.2.29_260528_x86_64_01.AppImage

# 4. 启动
bash scripts/start_qq.sh
```

所有产出（patched QQ、feed 归档、CLI 输出）都在项目目录内，复制/删除整个项目即可迁移/清理，不会在 `$HOME` 留下残留。

> **Python 包导入**：采用 src layout，`python3 -m prometheus.*` 需要 `PYTHONPATH=src`（或 `pip install -e .`）。bash 脚本不依赖此设置——它们直接按路径调用 `_envconfig.py`。

## systemd 部署

`deploy/qq-prometheus.service` 提供了 systemd user service 模板（默认禁用）。可根据需要启用以实现开机自动启动。
