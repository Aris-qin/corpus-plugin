# plugins/fact-infra/src/

fact-infra 插件的 TypeScript 源码(自 Python v0.3.0 迁移)。

## 结构

| 文件 | 职责 |
|---|---|
| `index.ts` | 插件入口:`defineToolPlugin` 注册 19 个 `fact_*` 工具 |
| `handlers.ts` | 工具 handler:参数校验 → store 调用 → `{ok:true,...}` / `{error:...}` |
| `store.ts` | SQLite 存储层(node:sqlite `DatabaseSync`,busy_timeout=30000,连接缓存) |

## 存储

- 中央索引:`~/.openclaw/fact-index.db`(项目注册表 + 期刊画像)
- 每项目:`projects/<slug>/fact.db`(tasks/decisions/issues/experiments/goals/events/meta)
- 项目目录必须**已存在**(`validateProjectDir` 拒绝自动创建,防误建)

## 工具分类

engineering / paper / review / revision / grant / patent / clinical / infra(项目类型)
verdict: conclude / partial / fail / pending(实验结论)

## 构建与测试

```bash
npx tsc -p tsconfig.json        # → dist/src/,dist/test/
node --test dist/test/*.test.js # 18 个单测
openclaw plugins validate --entry ./dist/src/index.js
```

## 验证

```bash
bash test/smoke.mjs             # 19/19 工具注册 + 8 工具链真实调用
```