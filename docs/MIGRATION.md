# MIGRATION.md — 迁移指南（阶段 3 产物）

> 状态: **草案 v0.1** | 2026-09-06 | 配套: docs/ARCHITECTURE.md
> 目标: 从 workspace 旧路径 → 新仓库 corpus-plugin 的完整迁移动作清单。
> **约束**: 迁移全程不改 openclaw.json / 不重启 gateway / 不装插件。生产数据先备份再动。

---

## 1. 路径对照表（旧 → 新）

| 旧路径（workspace，生产） | 新路径（corpus-plugin 仓库） | 说明 |
|---|---|---|
| `/home/node/.openclaw/workspace/scripts/corpus/` | `corpus/` | Python 核心引擎（已搬运 ✅ 阶段 1） |
| `/home/node/.openclaw/workspace/scripts/corpus/plugin/chunk_query.py` | `corpus/query.py`（并入 worker） | 双后端合一（架构 §3） |
| `/home/node/.openclaw/workspace/scripts/corpus/plugin/` | `plugins/corpus-query/` | TS 插件（已搬运 ✅） |
| `/home/node/.openclaw/workspace/agents/plugins/fact-infra/` | `plugins/fact-infra/` | fact-infra（已搬运 ✅，不联动） |
| `/home/node/.openclaw/workspace/projects/_corpus/corpus.db` | 保持原位（config 指向） | **229MB 生产数据，不搬迁，配置引用** |
| `/home/node/.openclaw/workspace/projects/_corpus/raw/` | 保持原位 | 原始文献（config 指向） |
| `scripts/corpus/plugin/node_modules/.../vec0.so` | `pip install sqlite-vec`（auto 探测） | 消除 npm 路径依赖（架构 §4.3） |
| `scripts/corpus/cli.py.bak-pre-qwen-rename-133238` | 不入库（保留在源） | 已废弃备份 |
| `scripts/corpus/chunker_markdown.v1.py.bak` | 不入库（保留在源） | 已废弃备份 |

---

## 2. 迁移动作清单（按顺序执行）

### Phase 0 — 备份（必须先做）
```bash
# 生产库备份（229MB）
cp /home/node/.openclaw/workspace/projects/_corpus/corpus.db \
   /home/node/.openclaw/workspace/projects/_corpus/corpus.db.bak-$(date +%Y%m%d)
# raw 目录只读校验（确认原文件在）
ls /home/node/.openclaw/workspace/projects/_corpus/raw/ | wc -l
```

### Phase 1 — 环境恢复（Python 侧,一次）
```bash
# 恢复 sqlite-vec（当前 python3.11.2 无此包，验证过）
pip install --break-system-packages sqlite-vec
# 验证 vec0 可加载
python3 -c "import sqlite_vec; print(sqlite_vec.get_loadable_path())"
```

### Phase 2 — 配置层落地（M1）
- [ ] `corpus.toml` 创建于 `~/.openclaw/corpus/corpus.toml`（或 `$CORPUS_HOME`）
- [ ] `[paths] corpus_db` 指向生产库绝对路径
- [ ] `[paths] vec_ext = "auto"`
- [ ] 6 处硬编码消除验证: `grep -rn "/root/.openclaw\|scripts/corpus" corpus/ plugins/corpus-query/src/` → 0 命中

### Phase 3 — 数据层迁移（M2/M4）
- [ ] `init` 或启动自检: 旧 corpus.db v2 schema 自动迁移到 v3（ALTER 幂等）
- [ ] 验证: `sqlite3 corpus.db ".schema documents"` 含 `content_hash/generation`
- [ ] `fsck` 跑一次: chunks↔vec0 一致、无孤儿 → **迁移前基线**
- [ ] embedding 维度校验: 现有向量是否 1024（qwen v4）→ 与 config.embedding.dim 一致
- [ ] 若不一致: 跑 `reembed_project.py` 全量重嵌（期间旧代可读）

### Phase 4 — 评分公式切换（M5）
- [ ] `corpus recompute-scores --formula v2` 全量回算
- [ ] 抽样验证: 取 3 篇已评分文献, 手工核对 `0.7×evidence+0.3×prior` 数值
- [ ] 确认 `quality_status ∈ {evidence, prior_only, partial}` 写正确

### Phase 5 — 在线召回切换（M3）
- [ ] `corpus worker --socket /tmp/corpus-worker.sock` 拉起
- [ ] `health` 自检: vec0 加载 + 维度 OK
- [ ] TS 插件 5 工具逐一调用验证（走 worker）
- [ ] 降级验证: 杀掉 worker → 工具自动 fallback spawn → 恢复 worker → 回切
- [ ] 24h 观察期后, 移除 spawn 硬编码（保留 feature flag）

### Phase 6 — 兼容期收尾
- [ ] 旧 CLI 直接调用路径（skills 里的 `corpus process-raw` 等）确认仍可用（别名兼容）
- [ ] `docs/SKILL-corpus-prisma.md` 命令引用更新（process-pdf → process-document 等）
- [ ] 确认 fact-infra 完全未动（产品边界）

---

## 3. 回滚方案

| 阶段 | 回滚动作 |
|---|---|
| Phase 0-4 | 恢复 corpus.db.bak-<date>（数据）; config 指回旧路径 |
| Phase 5 | 停 worker; 插件 `CORPUS_QUERY_MODE=spawn` 强制降级（代码保留旧路径）|
| 全部 | git revert（仓库内代码） + 恢复生产库备份 |

> 回滚原则: 数据先行、配置次之、代码最后。任何阶段回滚不应破坏现有 corpus 使用（生产在跑）。

---

## 4. 风险与依赖（迁移特有）

| 风险 | 影响 | 缓解 |
|---|---|---|
| corpus.db 229MB 迁移中损坏 | 生产不可用 | Phase 0 备份必做; 迁移只 ALTER 加列不重建表 |
| 现有向量维度 ≠ 1024 | 召回失效 | Phase 3 维度校验先行; 需要时 reembed |
| vec0.so auto 探测失败 | worker 起不来 | `vec_ext` 支持显式路径兜底（架构 §4.3）|
| 双后端合并后 chunk_query 行为漂移 | get/similar 回归 | 阶段 4 M6 补 get/similar 单测, 对比迁移前后输出 |
| skill 仍指向旧命令 | agent 调用出错 | Phase 6 同步更新 SKILL; 别名兼容期 |

---

## 5. 验收（迁移成功定义）

1. `grep -rn "scripts/corpus" corpus/ plugins/` = 0（TS 侧）/ 仅注释
2. worker health 通过 + 5 工具全绿
3. fsck 0 orphan（与 Phase 3 基线一致）
4. 评分 v2 数值抽查通过
5. 降级/回滚演练各跑通一次
6. 生产 corpus 使用（PRISMA 流水线）无感知切换

---

*下一步: 进入阶段 4（代码书写 v0.2.0+），按 ARCHITECTURE.md §11 里程碑 M1→M7 实施。*