# MemVid 作为 reading-graph 第二存储方案调研

> **日期**: 2026-07-04
> **目的**: 评估 MemVid (v2.0.140, Apache 2.0, Rust) 能否替代或互补当前 spiderweb+reading-graph 的存储架构
> **状态**: 📝 设计调研，非决策

---

## 一、当前架构 & 与 MemVid 的零件级对比

### 1.1 读书郎当前存储栈

```
┌─────────────────────────────────────────────┐
│              读书郎 (reading-graph)          │
├─────────────────────────────────────────────┤
│ CLI 工具 (reading-graph, Python)             │
│   ├── SQLite (doc_index.db)                 │
│   │   ├── FTS5 + jieba (wangfenjin/simple)  │
│   │   ├── vec_chunks (sqlite-vec)           │
│   │   ├── entity_aliases/entity_relations   │
│   │   └── interest_points / query_history   │
│   ├── Voyage voyage-4-large (embedding)     │
│   └── Voyage rerank-2.5-lite (reranker)     │
├─────────────────────────────────────────────┤
│ MCP 服务器 (spiderweb, Python)              │
│   ├── SQLite (spiderweb.db)                 │
│   │   ├── docs / chunks / sections          │
│   │   ├── fts + vec                         │
│   │   └── entities / insights               │
│   └── LLM fallback: DeepSeek→SenseNova→CF   │
├─────────────────────────────────────────────┤
│ Obsidian Vault (~/notebooks/wiki/)          │
│   └── Markdown 文件（可读、可 git、可编辑）  │
└─────────────────────────────────────────────┘
```

### 1.2 MemVid 存储栈

```
┌─────────────────────────────────────┐
│           MemVid (Rust)             │
├─────────────────────────────────────┤
│ 单文件 .mv2（自描述二进制格式）      │
│   ├── Tantivy BM25 (lex)            │
│   ├── HNSW 向量索引 (vec)           │
│   ├── MemoryCard 结构化存储         │
│   ├── Logic-Mesh 图谱 (NER)         │
│   ├── Time Index (时间序列)         │
│   └── Zstd/LZ4 压缩帧               │
├─────────────────────────────────────┤
│ Embedding: ONNX 本地 / OpenAI API   │
│ SDK: Rust / Python / Node.js / CLI  │
└─────────────────────────────────────┘
```

---

## 二、零件互换矩阵：两边能互相借什么

### 2.1 读书郎 → MemVid（可借给 MemVid 的能力）

| 组件 | 移植方式 | 难度 | 价值 |
|------|---------|:----:|:----:|
| **jieba 中文分词** → Tantivy tokenizer | 写 Rust crate `tantivy-jieba`，封装 jieba-rs，实现 Tantivy `Tokenizer` trait | 🔴 高（Rust FFI） | ⭐⭐⭐ — 让 MemVid 中文全文搜索可用 |
| **Voyage 中文 embedding** → MemVid `api_embed` | 直接配置：`base_url=https://api.voyageai.com/v1` + `model=voyage-4-large`。MemVid 已支持 OpenAI 兼容 API | 🟢 **零代码，即刻可用** | ⭐⭐⭐⭐⭐ — 解决了 MemVid 的中文语义搜索短板 |
| **LLM NER 实体提取** → 替代 Logic-Mesh DistilBERT | 把 Logic-Mesh 的 ONNX NER 替换为 DeepSeek 硅基流动的 LLM API 调用 | 🟡 中等 | ⭐⭐⭐ — 中文实体识别质量飞跃，但增加 API 成本 |
| **PaddleOCR 扫描件处理** | MemVid 已有 PDF 提取，但无 OCR。硅基流动 PaddleOCR-VL-1.5 API 可独立调用 | 🟢 简单 | ⭐⭐ — 仅针对扫描版书籍场景 |
| **reranker 重排序** | MemVid 目前无 reranker，可在查询后加一层 Voyage rerank-2.5-lite | 🟡 中等 | ⭐⭐⭐ — 提升搜索命中率 |

### 2.2 MemVid → 读书郎（可借给读书郎的能力）

| 组件 | 移植方式 | 难度 | 价值 |
|------|---------|:----:|:----:|
| **HNSW 向量索引** → 替代 sqlite-vec | `hnswlib` Python 库，在现有 `doc_index.db` 旁维护独立 `.hnsw` 文件。search 时查 HNSW，降级回 sqlite-vec | 🟡 中等（Python 兼容） | ⭐⭐⭐⭐ — 搜索快 10-100x |
| **MemoryCard 版本化** → 观点演化精细追踪 | 在 `interest_points` 表上追加 `version_relation` 列 + `parent_id` 实现 Sets/Updates/Retracts | 🟡 中等 | ⭐⭐ — 读书郎不需要 per-card 版本审计 |
| **CLIP 图像搜索** → 古籍插图检索 | MemVid 的 CLIP feature 可独立部署，需要先 OCR 提取书内图片 + 跑 embedding | 🟡 中等 | ⭐⭐ — 场景特定（有插图的古籍） |
| **加密胶囊 .mv2e** → 读书笔记加密分享 | MemVid 的 Argon2+AES-GCM 加密，导出为 `.mv2e` 文件 | 🟢 简单 | ⭐ — 不如发 Markdown 文件实用 |
| **时间旅行 Time Index** → 回溯阅读历史 | 读书郎已有 git commit + Obsidian 版本历史，不需要专用 Time Index | ❌ 已有等效方案 | ⭐ — 不增加新能力 |

---

## 三、最有价值的交叉点：详细方案

### 3.1 📌 方案 A：Voyage embedding → MemVid（即刻可行）

MemVid 的 `api_embed` feature 原生支持 OpenAI 兼容 embedding API，而 Voyage API 完全兼容该协议。

**步骤：**

```toml
# ~/.config/memvid/config.toml
[embedding]
provider = "api"
api_base = "https://api.voyageai.com/v1" 
model = "voyage-4-large"
api_key = "${VOYAGE_API_KEY}"
dimensions = 1024
```

**验证方法：**

```bash
memvid embed --input "人工智能的哲学基础" --db book.mv2
memvid search --query "图灵测试" --db book.mv2
```

**效果：** MemVid 即刻获得中文语义搜索能力（Voyage 支持中英文双语），无需等社区加中文 ONNX 模型。

### 3.2 📌 方案 B：HNSW 向量索引 → 读书郎（中高价值）

当前读书郎的向量搜索走 sqlite-vec，每次搜索做全库余弦距离计算（O(n)）。HNSW 降为 O(log n)。

**设计：**

```python
# 在 reading-graph 中新增 HNSW 索引模块
# ~/.hermes/util/reading-graph 新增 hnsw_index.py

import hnswlib
import numpy as np

class HNSWIndex:
    """旁路 HNSW 索引，与 sqlite-vec 共存"""
    
    def __init__(self, dim=1024, index_path="hnsw_index.bin"):
        self.dim = dim
        self.index_path = index_path
        self.index = hnswlib.Index(space='cosine', dim=dim)
        
    def build_or_load(self, vectors, ids):
        if os.path.exists(self.index_path):
            self.index.load_index(self.index_path)
        else:
            self.index.init_index(max_elements=len(ids), ef_construction=200, M=32)
            self.index.add_items(vectors, ids)
            self.index.save_index(self.index_path)
    
    def search(self, query_vector, top_k=10):
        labels, distances = self.index.knn_query(query_vector, k=top_k)
        return labels, distances
```

**集成点：** `reading-graph search` 中，优先查 HNSW 索引，降级回 sqlite-vec。

**效果：** 向量搜索从 ~200ms 降至 ~5ms（取决于索引大小和维度）。

### 3.3 📌 方案 C：jieba tokenizer → Tantivy（高投入高回报）

让 MemVid 的 Tantivy 全文索引支持中文分词。

**设计：**

```rust
// tantivy-jieba crate（新建）
use jieba_rs::Jieba;
use tantivy::tokenizer::{Tokenizer, Token, TokenStream};

pub struct JiebaTokenizer {
    jieba: Jieba,
}

impl Tokenizer for JiebaTokenizer {
    fn token_stream<'a>(&'a mut self, text: &'a str) -> Box<dyn TokenStream + 'a> {
        let words = self.jieba.cut(text, true);  // HMM 模式
        Box::new(JiebaTokenStream { words: words.into_iter(), text })
    }
}
```

**集成点：** MemVid 的 Tantivy schema 构建时注册 `JiebaTokenizer` 作为中文字段的 tokenizer。

**效果：** MemVid 支持中文全文搜索（"人工智能"匹配而非"人""工""智""能"单字）。

---

## 四、综合评估：要不要做？做什么？

### 4.1 不做整体替换

MemVid 不能替代读书郎的存储后端，原因：

1. **二进制 vs Markdown** — 读书郎的产出需要人肉眼可读，Markdown 不能放弃
2. **中文 NER** — Logic-Mesh 的 DistilBERT 是英文的，替换为 LLM NER 增加成本
3. **已有完整流水线** — 6 阶段入库 + 11 个 MCP 工具 + Hermes 插件，整体迁移不现实
4. **Rust 定制门槛** — 深度定制需要 Rust，而当前团队是 Python

### 4.2 选择性嫁接（推荐）

| 优先级 | 做法 | 投入 | 收益 | 路线图 |
|:------:|------|:---:|:----:|--------|
| 🥇 | Voyage embedding → MemVid | 0 代码，10 分钟配置 | MemVid 即刻支持中文语义 | **现在就可以试** |
| 🥈 | HNSW → 读书郎 search | ~2 天实现 | 向量搜索快 10-100x | 读书郎下个迭代 |
| 🥉 | jieba → MemVid Tantivy | ~1 周 Rust 开发 | MemVid 支持中文全文 | 有 Rust 资源时 |
| 4 | LLM NER → 替代 Logic-Mesh | 中等 | 中文实体识别 | 观察社区进展 |
| 5 | CLIP 图像搜索 | 中等 | 古籍插图检索 | 特定场景按需 |

### 4.3 架构示意图（推荐形态）

```
读书郎管道                                MemVid 互补
┌──────────┐     ┌──────────┐     ┌───────────┐
│ 拆书+OCR  │────▶│ FTS5     │────▶│ 向量搜索   │
│ (jieba)   │     │ (SQLite) │     │ (HNSW)    │← 嫁接 HNSW
└──────────┘     └──────────┘     └───────────┘
                       │                  │
                       ▼                  ▼
                 ┌──────────┐     ┌───────────┐
                 │ 实体图谱   │     │ 中文语义   │
                 │ (GBrain)  │     │ (Voyage)  │← 读书郎已有
                 └──────────┘     └───────────┘
                       │
                       ▼
                 ┌──────────┐
                 │ L3 观点   │
                 │ (Markdown)│
                 └──────────┘

MemVid 独立路径（实验性）
┌──────────────────────────┐
│ memvid ingest 英文书.mv2  │
│   + Voyage api_embed     │ ← 嫁接 Voyage
│   + (未来) jieba tokenizer│ ← 可选后期
└──────────────────────────┘
         │
         ▼
┌──────────────────────────┐
│ 英文/中文 .mv2 便携记忆   │
│ 可 git commit / scp      │
└──────────────────────────┘
```

---

## 五、附录

### 5.1 相关文件

| 文件 | 说明 |
|------|------|
| `ARCHITECTURE-ANALYSIS.md` | 当前 spiderweb 架构分析与问题诊断 |
| `DECISIONS.md` | spiderweb 架构决策记录 |
| `~/projects/spiderweb/domains/reading/config.yaml` | MCP server 的 LLM fallback 链配置 |
| `~/.hermes/skills/shun/reading-graph/SKILL.md` | reading-graph 完整技能文档 |
| `~/.hermes/skills/shun/book-ingest/SKILL.md` | 书籍入库技能文档 |

### 5.2 MemVid 项目信息

| 项目 | 内容 |
|------|------|
| 仓库 | https://github.com/memvid/memvid |
| 版本 | v2.0.140 |
| 许可证 | Apache 2.0 |
| 语言 | Rust 99.3% |
| 中文支持 | ❌ 严重短板（默认英文 embedding + 英文分词 + 英文 NER） |
| 最有价值特性 | HNSW 向量索引、MemoryCard 版本化、单文件便携 |
