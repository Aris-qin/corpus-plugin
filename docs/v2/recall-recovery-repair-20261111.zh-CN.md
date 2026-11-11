# 全局召回与真实进程中断恢复修复（2026-11-11）

## 边界与结论口径

仅修改授权候选 `/opt/data/corpus-build-20261110T170301Z/repo`，运行、HOME、TMP、合成库及证据均位于 `/opt/data/corpus-build-20261110T170301Z`。普通测试离线，使用现有 validation-venv；未访问生产、凭据、真实供应商或外部项目，未部署、重启、提交、推送、重嵌或修改认证/配置。保留恰好六工具、敏感动作默认拒绝、部分索引安全恢复以及旧数据 read/resolve 语义。

## P0：先相关性、后候选预算

已确认原实现对任意 ID/版本排序前缀执行 `LIMIT candidate_budget+1`，然后才进行 FTS、中文子串和向量相关性计算；这是真实的全局漏召回缺陷，而不是警告文字问题。

新实现逐行遍历完整 eligible 集合（保留原 active generation、canonical/merged work、生命周期、linked context、版本、DOI、年份与状态过滤）。FTS5 匹配分数按 ID 流式合并；中文子串回退检查所有 eligible 正文，不再仅检查前缀。每通道以最多 B 个元素的堆选择相关候选，保留 formal/preferred 优先级与相同分数的 ID 决胜顺序。向量通道对完整 eligible/profile 交集逐行计算精确 cosine，随后各通道有界排名进行 RRF，最多 2B 个 ID 合并，再将 downstream 候选限制为 B；原每版本最多五条、rerank 预算、证据回读和游标绑定仍保留。

成本说明：词法回退读取全部 eligible 文本；FTS 的按 ID 排序由 SQLite 管理，可能使用临时排序。精确向量计算是 O(N×D)，不是 ANN，也不因 B 小就只扫描 B 条。Python 排名堆 O(B)、合并 ID/候选正文 O(B)，单条向量 O(D)，没有全库 `fetchall` 或全库 Python dict/list。SQLite 内部缓存/临时排序及 OS RSS 不包含在 Python tracemalloc 观察中，不能据此声称生产内存或延迟上界。低 B 是相关性候选截断，并非对全部通道全排名融合后的数学最优结果保证。

六工具与参数不变；必要的响应语义修正：

- `candidate_count` 现在表示 downstream 相关候选数，不是任意前缀条数；旧的三行过滤场景现在报告 1 个匹配候选、3 个 eligible 扫描。
- `vector_candidates` 表示有界保留的向量候选数；总扫描量见 `retrieval_diagnostics.vectors_scanned`。
- `truncated` / `candidate_budget_exhausted` 表示相关性通道或融合候选超预算，不再仅因无关库很大而触发；无匹配返回真正的 no_matching_results。
- 新增 `retrieval_diagnostics`：coverage=full_eligible_corpus、eligible_chunks_scanned、lexical_matches、vectors_scanned、ranking_heap_limit、candidate_union_count。精确向量警告明确 full eligible、线性成本、有界内存。

## P1：故障机制、诊断与未证明部分

历史 sdist 首次 EOF 的 stderr 未完整保存，不能认定已定位该次故障的唯一根因。源码显示原测试仅给工作者 0.2 秒 lease；解析 checkpoint 和 activation 都检查 lease，超时会返回 failed/LEASE_LOST，子进程正常退出而不打印 readiness。新增确定性独立进程实验以 0.01 秒 lease、0.05 秒合成解析延迟重现这一机制：exit=0、stdout 包含 LEASE_LOST、无 readiness、旧 generation 完整，随后通过公共 jobs_control retry/run_one 成功恢复。这证明一个充分的故障机制，不证明历史事件就是这一原因；重复绿灯也不是历史根因证明。

真实 kill 测试现在显式使用当前解释器、`-I -B -u`、候选绝对 import root/cwd，清除 PYTHONPATH 和设置文件继承。stdout/stderr 都以 selector/os.read 有界收集，不使用可能卡住的 readline。10 秒 readiness 截止、64 KiB/流诊断限额、kill 后 2 秒 communicate 清理；每次记录 command、解释器版本、cwd、允许的非敏感环境、stdout、stderr、returncode、kill 前 returncode 与耗时到 child-execution.json。早退、正常退出却缺失 readiness、静默超时、半行超时均有确定性负测试，测试限定总耗时小于 5 秒。

工作者使用明确 2 秒 lease（不是把 readiness 当成成功替代）；到 checkpoint 时断言事务尚未提交、子进程内有两个 generation、active 指针仍为旧 generation；父进程在 kill 前看到的仍只有旧 generation，随后发送真实 SIGKILL，要求 kill 前仍存活、kill 后负 returncode。等待真实 lease 到期后以公共 run_one 重领并恢复，固定旧引用仍可读。没有模拟成功、跳过故障或绕过审批。2 秒仍可能在极慢环境耗尽，届时必须失败并留证，而非自动重试掩盖。

`__package__ != __spec__.parent` 的原因已独立重现：非 package 的目录入口 loader 触发代码补 `__package__/__path__`，但未将 spec 同步为 package。现同步 spec.submodule_search_locations；两种 loader 模式均断言 package/spec 一致，不屏蔽 DeprecationWarning。

## RED / GREEN 与证据

修复证据根：`/opt/data/corpus-build-20261110T170301Z/evidence/recall-recovery-repair-20261111`。

- `collection-red.log`：先审查 safe 测试收集，不收集旧 tests。
- `recall-red.log`：首次合成 fixture 试图删除 immutable chunks 的错误原样保留；修正为仅追加合成切片，没有关闭保护触发器。
- `recall-red-valid-fixture.log`：有效 fixture 下 7 项真正 RED，覆盖超过 10,000 条、前缀外唯一词、中文子串、标点、无匹配、向量最佳结果、linked/filter/rerank/分页。
- `crash-red.log`：5 failed / 1 passed；缺失诊断 helper 四项失败和 non-package spec 不一致一项失败。
- `targeted-green-first.log`：64 passed / 1 failed；新增测试错误地要求含到期时间的下一页游标字节完全相等。保留该失败，改为检验重复页面 hits 一致，未改游标实现或放松产品分页约束。
- `targeted-green-second.log`：67 passed；新增高基数合并/回收/版本优先级、真实 rerank runtime 离线 fixture 与故障机制重现均通过。

全矩阵命令：`bash tools/validate.sh --sandbox-root /opt/data/corpus-build-20261110T170301Z`。
矩阵证据目录：`/opt/data/corpus-build-20261110T170301Z/evidence/validation-2oC5Ky3w`；运行目录：`/opt/data/corpus-build-20261110T170301Z/tmp/validation-OBybbKT6`。

首次完整修复矩阵退出 0，无需重跑掩盖矩阵故障。脚本在每种形态 full suite 前单独收集，并分别执行 full、compile、20 次独立 pytest/真实子进程 crash；保留各次 log/exit/child JSON。还执行构建、独立 venv 离线安装、console 与目录入口六工具检查，均不是 Hermes SDK 真正发现验收。

| 交付形态 | full safe suite | full 耗时 | 独立真实 kill 压测 | compile |
| --- | --- | --- | --- | --- |
| source | 268 passed / 0 failed / 0 errors / 0 skipped | 19.21 秒 | 20/20 | 通过 |
| unpacked sdist | 268 passed / 0 failed / 0 errors / 0 skipped | 20.84 秒 | 20/20 | 通过 |
| unpacked wheel | 268 passed / 0 failed / 0 errors / 0 skipped | 19.22 秒 | 20/20 | 通过 |

这是相同 268 场景的三种形态，不是 804 个独立场景；每种 full 内还各含一次真实 kill，额外 stress 为每种 20 次。三种 full 日志均无 pytest warnings 摘要，原 package/spec DeprecationWarning 未再出现。每次 kill 的 JSON 证明 kill 前存活、负退出码及清理完成；超时与早退负例的输出和退出码保留在运行目录各形态 `*-tests` 下的 child-execution.json。

10,025 条 eligible chunk（其中 10,024 条两维向量）、B=2 的 tracemalloc 观察：source 精确检索 0.2648 秒，sdist 0.2564 秒，wheel 0.2718 秒，Python 峰值均 14,295 字节；原始记录为矩阵目录 `source-working-set.json`、`sdist-working-set.json`、`wheel-working-set.json`。这是本机小文本、两维离线向量的单次观察，不是 1024 维生产成本保证，也不包括 fixture 构建、SQLite 临时排序或 OS RSS。

`verified-results.json` 在修复证据根中汇总 JUnit 精确计数、60 次子进程断言及所有验证命令退出码。矩阵目录的 `artifact-sha256.txt` 保留已验证构建摘要；最终报告补记后，仅 docs 有变化，最终归档另行保存在修复证据根 `final-dist`，并核对所有代码/测试与已验证归档字节相同。

## 改动文件

- `corpus_core/retrieval.py`：完整 eligible 流式召回、有界通道堆、候选融合预算、明确诊断。
- `__init__.py`：目录入口 package/spec 元数据同步。
- `tests/safe/test_recall_repair.py`：8 个高基数召回、版本、合并/回收、分页、rerank 与工作集回归。
- `tests/safe/test_crash_diagnostics.py`：5 个确定性早退/缺失/超时/lease 机制负例。
- `tests/safe/test_crash_recovery.py`：有界 readiness、双流诊断、真实 checkpoint kill、真实 lease 到期恢复。
- `tests/safe/test_predeploy_repair.py`：两种目录 loader 的 package/spec 一致性断言。
- `tests/safe/test_bounded_integrations.py`：相关候选数与 eligible 扫描数分别断言。
- `tools/validate.sh`：三形态收集、20 次/形态独立 crash 与逐次证据保存。
- 本报告：成本、响应语义、RED/GREEN、历史故障证据缺口、数据限制和父级验收。

与父级预修复 manifest 比较的清单保存在修复证据根 `changed-files-review.json`；没有修改 schema、迁移、auth/settings、供应商实现或工具 contracts。

## 剩余数据限制及父级验收

旧原件托管证据、legacy_unknown 向量来源、历史未解决分类/关系均仍为数据/审批限制，不发明映射、provenance 或 verifier，不授权批量重嵌。正式发布、永久删除及敏感批/API 保持 default-deny。

父级仍需独立检查改动及重新运行针对性/full suite；按真实 42,225 searchable chunks 场景确认原唯一词全局/过滤一致，审查 O(N) 实际成本和候选预算策略，再决定后续索引设计与生产接管。真实供应商、真实旧库副本、Hermes SDK 必须由父级对本次精确候选另行执行；本轮没有执行，不能继承前一候选的 live 通过记录作为本次通过。生产替换仍不在授权范围。
