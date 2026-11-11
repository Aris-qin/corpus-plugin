# Corpus 最新交付候选：独立本地复验

日期：2026-11-11。

## 结论与边界

本轮隔离本地复验通过，未发现新增候选代码回归；不是完整上线授权，也不是旧库已完成语义接管或真人审批已实现。未部署、重启、提交 Git、修改生产插件、配置或正式数据库。

测试对象为项目交付目录 `/workspace/projects/corpus-plugin/development/corpus-recall-recovery-candidate-20261111/`。按其 delivery-sha256.json 的 64 个文件创建新的授权沙盒副本，测试前后交付原件与副本均符合清单，无源码修改。没有直接沿用上一轮测试结果。

执行证据根：`/opt/data/corpus-local-recheck-20261111T102909Z/`。

## 全套离线、打包与中断恢复

源码 268 passed，0 failed/errors/skipped；sdist 268 passed，0 failed/errors/skipped；wheel 268 passed，0 failed/errors/skipped。三种交付形式重复相同场景，不能加总为不同场景。

编译、构建、离线 wheel 安装、独立导入与安装后 CLI 通过。各形式额外 20 次真实进程激活前中断恢复，共 60 次通过；逐份核对 readiness、kill 前进程存活、kill 后退出码 -9，均有实际子进程证据。

本轮从项目交付的同字节内容复制到新的显式授权沙盒后打包，未将授权范围扩大到根目录，也未移除路径守卫。因此本轮不再包含上一轮直接从 /workspace 源码构建时的沙盒边界失败。

## Hermes 与真实供应商

使用宿主 `/opt/hermes/.venv/bin/python` 从隔离 HERMES_HOME/plugins 自动发现插件，未向候选根目录注入 sys.path/PYTHONPATH。enabled=true、error=null，六工具注册，真实 ingest handler 成功。

通过本轮候选实际私有运行时，完成两份明确合成资料的入库、切片、真实 text-embedding-v4@1024、全局检索、DashScope 原生 qwen3-rerank、带定位原文回读；共四个成功 embedding 批次（含查询），并成功 rerank。没有 fixture transport，没有更换模型、密钥或全库重建。

特意限制预算，分别得到 1 条和 0 条已完成向量的 partial 任务，再通过公开 retry 入口恢复为 succeeded。切片、解析缓存和已有向量均保留，没有重复已完成向量。

六工具均实际调用；草稿保存、关联、回收通过。正式报告发布与永久删除实际调用返回 SENSITIVE_ACTION_DISABLED，这是安全拒绝，不是批准功能通过。

## 历史数据库副本迁移、恢复与召回

输入为已保留的真实旧库历史快照，immutable/read-only 打开，不访问当前生产数据库。迁移前后输入快照字节哈希一致。

14 张源表的全部原始字段按 SQLite/Python 类型及重复行计数逐行核对一致。418 份资料、42,548 个切片、42,498 条向量保留；旧 ID 集合与新引用映射完整，样本 resolve/read 通过。重复迁移零新增写入，integrity_check=ok，无外键错误。

执行目标数据根备份、加入合成资料、恢复后核对全部表计数与 typed 内容摘要、托管对象哈希，新增标记消失，搜索与读取正常。这是隔离恢复演练，不是生产切换。

原漏检查询 `abletooperateforalongperiodoftime`，candidate_budget=2，完整扫描 42,225 条可用切片，全局与指定文献查询命中同一原文，回读包含查询。一次词法查询约 0.302 秒，只是本机快照观察，不是性能承诺。旧向量未认领来源，本项为词法召回，不是旧向量语义接管。

## 保留的失败与限制

独立召回 harness 首次启动因父进程未设 CORPUS_AUTHORIZED_SANDBOX_ROOT 返回 ROOT_REQUIRED，尚未进入检索。补齐隔离环境变量后通过；首次日志 recall.log 与成功日志 recall-retry.log 分别保留。未修改产品代码绕过守卫。

仍有 418 份 external_unverified 原件、42,498 条 legacy_unknown 向量（含 2 条孤立向量）、9,812 项未解决历史关系；15 份 self_written/未知资料保持检索隔离。缺身份证据的原件收养、缺可信来源的向量认领均被安全拒绝，没有猜补。

真人批准适配尚未接通，正式报告发布、批量正式采用、合并、永久删除仍默认禁用。不能宣称完整产品能力已验收。历史偶发中断测试的唯一根因仍未证实，本轮 60 次成功不追溯证明历史根因。

## 后续决策

候选代码、受限模式与本轮测试范围可接受；正式替换前仍需决定旧资料托管及旧向量可信来源认领方案，并确认是否接受敏感动作禁用的受限部署。本次请求只授权本地验证，没有执行部署。

完整结构化汇总：`/opt/data/corpus-local-recheck-20261111T102909Z/summary.json`。
