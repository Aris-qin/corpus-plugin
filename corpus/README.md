# corpus/ — Python 文献库流水线

> SQLite + sqlite-vec 的统一文献库管理(检索 / 切片 / 评分 / 入库)

> ⚠️ **v0.1.0 脚手架阶段**:此 README 描述**现有功能**(从 `workspace/scripts/corpus/` 搬迁而来),不代表最终设计。重构方案见 `docs/ARCHITECTURE.md`(阶段 3 产物)。

## 模块清单

| 文件 | 行数 | 职责 |
|---|---:|---|
| `cli.py` | 1756 | 主 CLI 入口,8 个子命令: `init` / `ingest` / `query` / `score` / `group` / `list-projects` / `list-groups` / `process-raw` / `reembed-project` |
| `db.py` | 596 | SQLite + sqlite-vec schema 定义;5 张表:`documents` / `venue` / `chunks` / `relevance` / `quality_evidence` |
| `chunker_markdown.py` | 686 | Markdown 切片器(tree-aware,保留 heading 层级) |
| `jats_to_md.py` | 145 | JATS XML → Markdown 转换 |
| `openalex.py` | 111 | OpenAlex 元数据接入 |
| `reembed_project.py` | 161 | 重新 embedding 已有 chunks |
| `migrate_v2.py` / `migrate_v2.sql` | 85 / 41 | schema 迁移 |
| `a2a_wake.sh` | 56 | A2A 接力棒 helper(agent 间交接) |
| `tests/test_chunker*.py` | 424 | 切片器单元/集成测试 |

## 数据库结构

每个项目一个 corpus 实例:
```
projects/<project>/corpus/metadata.sqlite
```

5 张表:
- **`documents`** — 文献元信息(pmid/title/doi/source/article_type/quality_type_prior)
- **`venue`** — 期刊 venue 信息(IF/JCR/CAS,可 unknown)
- **`chunks`** — chunk 文本 + heading_path + embedding (vec0)
- **`relevance`** — chunk 级相关度评分(待 curator 重算)
- **`quality_evidence`** — curator 重算的证据核验特征(3 项 + 综合分)

## 安装

```bash
cd corpus/
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 验证
corpus-cli --help
pytest tests/
```

## 已知问题(下一阶段重构对象)

1. **路径硬编码**:cli.py 中 `UNIFIED_CORPUS_DB = "projects/_corpus/corpus.db"` 是相对路径,依赖 cwd
2. **CLI_PATH 硬编码**:`plugins/corpus-query/src/index.ts` 中硬编码 `/root/.openclaw/workspace/scripts/corpus/cli.py`,新仓库路径变了
3. **embedding 调用分散**:cli.py 内多个位置直接调 `_get_embedding`,抽象层次不够
4. **错误处理不统一**:部分命令返回 dict,部分返回字符串,部分返回 exit code
5. **测试覆盖不足**:仅 chunker 有测试,db / cli 缺测试

## 相关文件

- `pyproject.toml` — Python 项目配置
- `migrate_v2.sql` — schema v2 迁移脚本
- `../docs/SKILL-corpus-prisma.md` — PRISMA 7 阶段流水线 SOP
- `../plugins/corpus-query/` — 此流水线的 OpenClaw plugin 包装
