# plugins/corpus-query/src/

corpus-query 插件的 TypeScript 源码。

## 结构

| 文件 | 职责 |
|---|---|
| `index.ts` | 插件入口:`defineToolPlugin` 注册 5 个工具;socket worker 优先 + spawn CLI 降级双通道 |

## 工具清单

| 工具 | 通道 | 说明 |
|---|---|---|
| `corpus_query` | socket `query` / spawn `cli query` | 混合检索(向量 KNN + 关键词 + 聚合) |
| `corpus_search_self` | spawn `cli search-self` | 自写文档 RAG(Qwen embedding + tree rerank) |
| `corpus_score` | socket `score` / spawn `cli score --score-event-id` | curator 评分(v3 幂等) |
| `corpus_get_chunk` | spawn `chunk_query.py get` | chunk 全文 + heading tree 上下文 |
| `corpus_list_similar_level` | spawn `chunk_query.py similar` | 同级 heading 跨文档相似检索 |

## 通道选择

- `CORPUS_QUERY_MODE=socket`(默认):Unix socket → Python worker(`corpus/worker.py`),
  常驻进程,无冷启动。
- `CORPUS_QUERY_MODE=spawn`:每次调用 spawn CLI,降级/测试用。

## 构建

```bash
pnpm install && pnpm build      # → dist/index.js
openclaw plugins build --entry ./dist/index.js   # 刷新 openclaw.plugin.json
```

## 验证

```bash
bash test/smoke.mjs             # 5/5 工具真实 execute(临时 DB)
```