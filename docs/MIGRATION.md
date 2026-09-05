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

> **顺序原则（codex 审查修订）**: 备份/体检 → 环境 → 配置 → 数据迁移 → 评分 → worker。任何破坏性动作前先完成一致性快照与 integrity 校验。

### Phase 0 — 一致快照 + 体检（必须先做）
```bash
# 1. 一致性备份（不用 cp!SQLite 在线 cp 可能不一致）→ 用 backup API / VACUUM INTO
python3 - <<'EOF'
import sqlite3
src = sqlite3.connect('/home/node/.openclaw/workspace/projects/_corpus/corpus.db')
dst = sqlite3.connect('/home/node/.openclaw/workspace/projects/_corpus/corpus.db.bak-$(date +%Y%m%d)')
src.backup(dst)
dst.close(); src.close()
EOF
# 2. 恢复演练: 该备份能独立打开 + integrity_check OK
python3 -c "import sqlite3; c=sqlite3.connect('...bak'); print(c.execute('PRAGMA integrity_check').fetchone())"
# 3. 迁移前体检: schema 版本 / 磁盘 ≥2× 库大小 / WAL checkpoint / vec0 载入
sqlite3 corpus.db "PRAGMA user_version; PRAGMA journal_mode; SELECT count(*) FROM chunks; SELECT count(*) FROM chunk_vectors;"
# 4. 记录基线: 行数 / schema / vec 维度 → 迁移后对比
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
- [ ] `[paths] corpus_db` 指向生产库绝对路径;`[paths] vec_ext = "auto"`;`[paths] retention_days = 7`
- [ ] 6 处硬编码消除验证: `grep -rn "/root/.openclaw\|scripts/corpus" corpus/ plugins/corpus-query/src/` → 0 命中

### Phase 3 — 数据层迁移（M2/M4）
- [ ] **先 fsck 基线**（迁移前）: chunks↔vec0 一致、无孤儿 → 结果存 `docs/review/fsck-baseline-<date>.txt`（失败**不迁移,先修**）
- [ ] 运行 v2→v3 幂等迁移（`PRAGMA table_info` 逐列检查 + user_version 版本链,§6.3）
- [ ] 验证: `sqlite3 corpus.db ".schema documents"` 含 `content_hash/generation`;quality_evidence_v3 行数 = 旧表行数
- [ ] 迁移后 fsck: 与基线一致（0 orphan）+ integrity_check OK
- [ ] embedding 维度校验: 现有向量是否 1024（qwen v4）→ 与 config.embedding.dim 一致;不一致 → `rebuild-vectors`（新代,旧代保留回滚窗）

### Phase 4 — 评分公式切换（M5）
- [ ] `corpus recompute-scores --formula v2` 全量回算（append-only,旧 v1 行保留）
- [ ] 抽样验证: 取 3 篇已评分文献, 手工核对 `0.7×evidence+0.3×prior` 数值 + quality_status 枚举正确
- [ ] 确认 prior_only / partial / override 推荐行为（默认不进 primary）生效

### Phase 5 — 在线召回切换（M3）
- [ ] `corpus worker --socket <config worker_socket>` 拉起（pidfile + flock 单实例验证）
- [ ] `health` 自检: vec0 加载 + 维度 + schema 版本 OK
- [ ] TS 插件 5 工具逐一调用验证（走 worker）
- [ ] 降级验证（**含失败注入**）: 杀 worker → 工具自动 fallback spawn → 重启 worker → 回切;中途断开连接/重复 score/超时 → 幂等表查询验证（score-status）
- [ ] 24h 观察期后, 移除 spawn 硬编码（保留 feature flag）

### Phase 6 — 兼容期收尾
- [ ] 旧 CLI 直接调用路径（skills 里的 `corpus process-raw` 等）确认仍可用（别名兼容）
- [ ] `docs/SKILL-corpus-prisma.md` 命令引用更新（process-pdf → process-document 等）
- [ ] 确认 fact-infra 完全未动（产品边界）

---

## 3. 回滚方案（按阶段,真回滚,非"指回旧路径"）

> 原则: **停止写入 → 恢复一致快照 → 校验 → 恢复服务**。新 schema 已写入数据库时,"config 指回旧路径"无法回滚已格式化的库——必须恢复 Phase 0 快照。

| 阶段 | 回滚动作 |
|---|---|
| Phase 0-1 | 无数据变更,直接重做 |
| Phase 2 | config 改回旧值即可 |
| Phase 3 | 停止写入 → 恢复 Phase 0 一致快照（backup API 产物）→ 重跑 fsck 验证 → 恢复服务（新代码仍可读旧 schema,兼容） |
| Phase 4 | 恢复快照（丢弃 v2 评分行;v1 行仍在快照里）或跑 `recompute-scores --formula v1`（不推荐,快照更干净） |
| Phase 5 | 停 worker → `CORPUS_QUERY_MODE=spawn` 强制降级（代码保留旧路径）→ 验证 spawn 通路正常 |
| 全部 | 仓库代码 git revert;生产库恢复快照;raw/canon 目录不受影响（canon 可再生,raw 只读） |

> 备份保留期: 快照保留 ≥30 天 / ≥2 份轮换（回滚窗口 7 天旧代数据 + 30 天快照双保险）。每次回滚演练留记录。

---

## 4. 风险与依赖（迁移特有）

| 风险 | 影响 | 缓解 |
|---|---|---|
| corpus.db 229MB 迁移中损坏 | 生产不可用 | Phase 0 一致快照必须（backup API,非 cp）+ integrity 基线;迁移只加列不重建表 |
| 现有向量维度 ≠ 1024 | 召回失效 | Phase 3 维度校验先行;需要时 rebuild-vectors（新代,旧代回滚窗）|
| vec0.so auto 探测失败 | worker 起不来 | `vec_ext` 显式路径兜底（架构 §4.3）|
| 双后端合并后 chunk_query 行为漂移 | get/similar 回归 | 阶段 4 M6 补 get/similar 单测 + 迁移前后输出对比 |
| skill 仍指向旧命令 | agent 调用出错 | Phase 6 同步更新 SKILL;别名兼容期 |
| ALTER 非幂等（SQLite 无 IF NOT EXISTS） | 重复迁移报错 | §6.3 迁移契约: PRAGMA table_info 逐列检查 + user_version 版本链 |
| vec0 虚拟表无法 ALTER(加 generation 列) | 迁移失败 | generation 走 chunk_id 前缀,不 ALTER vec0（架构 §6.1）|
| 备份 cp 在线不一致 | 回滚库损坏 | Phase 0 强制 backup API / VACUUM INTO,恢复演练验证 |
| 评分 v1/v2 行混淆 | 显示错误评分 | quality_evidence_v3 append-only + current 指针 + fsck 指针唯一性校验 |

---

## 5. 验收（迁移成功定义,含失败注入）

1. `grep -rn "scripts/corpus" corpus/ plugins/` = 0（TS 侧）/ 仅注释
2. worker health 通过 + 5 工具全绿（含 score JSON 响应,无正则）
3. fsck 0 orphan（与 Phase 3 基线一致）;integrity_check OK
4. 评分 v2 数值抽查通过（3 篇手工核对）;quality_status 枚举正确
5. 降级/回滚演练各跑通一次（含失败注入: 杀 worker 中途断开 / 重复 score 幂等 / socket stale 清理 / 磁盘满模拟）
6. 生产 corpus 使用（PRISMA 流水线）无感知切换
7. 迁移幂等验证: 迁移脚本重跑一次 = 无变化（0 新增列/行）

---

*下一步: 进入阶段 4（代码书写 v0.2.0+），按 ARCHITECTURE.md §11 里程碑 M1→M7 实施。*