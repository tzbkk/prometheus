# 派生分析工具链（非 Prometheus 本体）

从 `data/{guild}/` 实体树再生帖子级分析产物：清洗、主题聚类、粗类重鉴定、趋势图。
所有输出写入仓库根的 `analysis/`（已 gitignore，可随时再生）；本目录脚本不参与 launcher，
不被 `src/` 引用。

## 环境

- Python：仓库 `.venv`（依赖 `jieba`、`matplotlib`、`numpy`、`Pillow`、`bertopic` 等）
- 本地多模态模型：OpenAI 兼容服务 `http://127.0.0.1:1234/v1`，模型 id `qwen/qwen3.5-9b`
  （视觉 + 文本；`reclass_llm.py` / `reclass2.py` 依赖）
- 远程金标模型（可选）：智谱 `glm-5.3-flash`，需 `ZAI_APIKEY`；当前余额不足，仅用于历史上限评估

llama.cpp 启动示例（LM Studio 自带后端 + Qwen3.5-9B GGUF）：

```bash
export LD_LIBRARY_PATH=$HOME/.lmstudio/extensions/backends/llama.cpp-linux-x86_64-nvidia-cuda12-avx2-2.60.0:$HOME/.lmstudio/extensions/backends/vendor/linux-llama-cuda12-vendor-v1
llama-server -m ~/.lmstudio/models/lmstudio-community/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf \
  --mmproj ~/.lmstudio/models/lmstudio-community/Qwen3.5-9B-GGUF/mmproj-Qwen3.5-9B-BF16.gguf \
  --port 1234 --host 127.0.0.1 -ngl 99 -c 49152 --parallel 8 --jinja \
  --reasoning-budget 0 -fa on --cache-type-k q8_0 --cache-type-v q8_0
```

## 分类体系（8 粗类 + 5 正交标签）

行为轴互斥、按主导功能归类；标签可叠加。依据：Fiske 三重生产力、Jenkins 参与式文化四形态、
OECD UCC 判据、Bruns gatewatching / Lee 文化中介、Li/He/Hu 求供四分、Whyke 谷子产消、
Carey 仪式观 / Malinowski 寒暄交际 / Muñiz & O'Guinn 社群仪式、Kraut & Resnick 社群维系。

| 粗类 | 定义 | 学术锚点 |
|---|---|---|
| 同人创作 | 自己产出的新内容：文/画/剪辑/翻唱演奏/手作/AI 成品 | Fiske 文本生产力；OECD creative effort |
| 搬运策展 | 转发既有内容且帖文仅有来源/标题/署名；附实质评论归言说讨论 | Bruns gatewatching；Lee 文化中介 |
| 求助与供享 | 求图/求资源/求软件/求歌名；图包、网盘资源包发放 | Li/He/Hu 求-供四分 |
| 谷子交易 | 周边购买、到货、出价、出物、换谷、晒谷 | Whyke 产消三联 |
| 言说讨论 | 针对作品/剧情/角色/画面的实质评价与讨论 | Fiske 言说生产力；Busse & Gray |
| 仪式寒暄 | 无信息增量的问候、打卡、节日祝福、表情包寒暄 | Carey 仪式观；Malinowski；Muñiz & O'Guinn |
| 社区运维 | 频道公告/活动/规则/世萌应援/频道 AI 日报 | Kraut & Resnick；OCCB |
| 自我展演 | 第一人称实质性生活与情感叙事（有具体经历/感受） | Naaman ME/AM；Fiske 符号生产力 |

标签：`定向互动`（@/回复/点名）、`AI生成`、`节日节点`、`跨圈主体`、`官方信源`。

规范全文：`analysis/reclass_shards/SPEC2.md`（随 `--build-inputs` 生成）。

## 脚本

| 脚本 | 作用 | 常用命令 |
|---|---|---|
| `topic_trends.py` | 清洗 → 嵌入/聚类 → 命名 → 专项规则 → 合并 → 图表；含角色维度 | `python scripts/analysis/topic_trends.py --stage chart --top 20` |
| `reclass_llm.py` | v1：29 细类多模态重鉴定（帖文+评论+≤2 图，低置信升级队列） | `python scripts/analysis/reclass_llm.py --build-inputs` |
| `reclass2.py` | v2：8 粗类 + 5 标签；规则层 + LLM；金标对比 | `python scripts/analysis/reclass2.py --workers 12 --tag main4 --mode cat` |
| `coarse_chart.py` | 8 粗类逐月双 stack 图（绝对量 + 占比），图例含帖数与关键词 | `python scripts/analysis/coarse_chart.py` |

### reclass2.py 细节

- 模式：`--mode cat`（只输出类别名，6 output tokens，快 2.4 倍）与 `--mode json`（带 tags/置信度）
- 采样：`--input gold200_sample.jsonl`（任意输入集）、`--gold`（58 条旧金标）、默认全量 `inputs.jsonl`
- 规则层：AI 日报 / 世萌 / 公告 / 谷子 / 求助 / 搬运 / 组队打卡 / 同人公告 直接出结果，跳过 LLM
- 升级：输出含多个类别名或无法解析 → `escalation_<tag>.jsonl`
- 评估：`--compare --tag <tag>`（对远程金标）、`--coarse-stats`（旧细类→粗类分布与映射导出）

## 数据流

```
data/{guild}/ 实体树
  └─ docs_clean.jsonl（帖文+评论≤30 条，<6 字过滤）
      ├─ 规则分类 → doc_categories.json
      ├─ reclass_llm / reclass2（多模态）→ analysis/llm_cache/reclass_<tag>/
      └─ 合并 → coarse_labels.jsonl → coarse_trend.csv / coarse_summary.csv / coarse_trend_dual.png
```

## 输出（analysis/，gitignored）

| 文件 | 内容 |
|---|---|
| `docs_clean.jsonl` | 清洗后的帖子文档 |
| `doc_categories.json` | 旧 29 细类逐帖映射 |
| `coarse_labels.jsonl` | 8 粗类 + 标签 + 判定来源（规则/LLM/人工） |
| `coarse_trend.csv` | 逐月粗类计数 |
| `coarse_summary.csv` | 粗类规模、占比、关键词、峰值月 |
| `coarse_trend_dual.png` | 月度绝对量 + 构成占比双 stack 图 |
| `character_trends.png/csv` | 角色提及占比趋势 |
| `topic_summary_merged.csv` / `topics_over_time_merged.csv` | 旧 29 细类汇总与逐月构成 |
| `llm_cache/` | 逐帖 LLM 判定缓存（可断点续跑） |
| `reclass_shards/` | 输入分片、金标样本、SPEC 与裁决结果 |

## 质量

- 金标：206 条分层样本，`glm-5.3-flash` 两轮 + 分歧第三轮裁决；远程两轮自一致率 87–89%
- 本地 9B + 规则层：**70.5%**（178 条非争议样本）；纯 LLM 63.5%；旧 29 细类 53.4%
- 分类别：社区运维 92% / 言说讨论 82% / 求助 71% / 仪式 70% / 谷子 69% / 搬运 68% / 创作 68% / 自我展演 58%
- 性能：9,678 帖 / 39 分钟 / 4.13 帖/s（RTX 4060 Ti 16G，8 槽 × 6k ctx，Q8 KV）
- 已知噪声：58 条小样本上变体差异（分辨率/输出格式）在 ±2.5pp 内，不可据此调参；月度小样本月份（<20 帖）占比波动大，趋势以上图绝对量为准
- 远程账户余额不足时（智谱 `1113`），107 条升级样本以本地宽松解析 + 6 条人工裁定收尾（占 1.1%）
