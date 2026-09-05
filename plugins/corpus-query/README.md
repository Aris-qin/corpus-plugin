# plugins/corpus-query

> 5 个 corpus_* 工具的 OpenClaw TS Plugin,基于 `corpus/cli.py` Python CLI 桥接

> ⚠️ **v0.1.0 脚手架阶段**:此 README 描述现有功能,不代表最终设计。

## 工具清单(5 个)

| 工具名 | 用途 |
|---|---|
| `corpus_query` | 混合检索 + 项目分组过滤 + 3 维评分排序 |
| `corpus_search_self` | 自写文档检索(self_written source) |
| `corpus_score` | 单篇文献 3 维重打分(evidence features) |
| `corpus_get_chunk` | 获取 chunk 的完整 tree context(parent / children / siblings) |
| `corpus_list_similar_level` | KNN over chunks at same heading level |

## 文件结构

```
plugins/corpus-query/
├── README.md                       ← 你在这里
├── RESULT.md                       ← 2026-08-16 验收报告
├── openclaw.plugin.json            ← plugin manifest(声明 5 个 tool)
├── package.json                    ← npm 配置
├── tsconfig.json
├── pnpm-workspace.yaml
├── pnpm-lock.yaml
├── chunk_query.py                  ← Python helper(被 src/index.ts 调用)
└── src/
    └── index.ts                    ← definePluginEntry + spawn Python CLI
```

## 工作原理

`src/index.ts` 是一个 **Python 桥接器**:
1. 接收 agent 的 tool call 参数
2. 用 `spawn("python3", [CLI_PATH, ...args])` 调用 `corpus/cli.py` 或 `chunk_query.py`
3. 把 stdout 作为 JSON 返回给 agent
4. 退出码非 0 时把 stderr 透传出去

**当前硬编码路径**(阶段 4 重构对象):
```typescript
const CLI_PATH = "/root/.openclaw/workspace/scripts/corpus/cli.py";
const CHUNK_HELPER_PATH = "/root/.openclaw/workspace/scripts/corpus/plugin/chunk_query.py";
```

## 安装

```bash
cd plugins/corpus-query
pnpm install
pnpm run build
openclaw plugins build --entry ./dist/index.js
openclaw plugins validate --entry ./dist/index.js
openclaw plugins install "$(pwd)"
```

## 已知问题

1. **CLI_PATH 硬编码**:搬迁后旧路径失效
2. **CHUNK_HELPER_PATH 硬编码**:同上
3. **错误处理**:Python helper exit code 1 时,TypeScript 保留 stdout 错误信息(已修)
4. **5 个 tool 与 manifest 同步**:RESULT.md 修复过,需回归测试

## 相关文档

- `RESULT.md` — 2026-08-16 验收报告(PLUGIN_OK)
- `../../corpus/` — Python CLI 流水线
- `../../corpus/README.md`
