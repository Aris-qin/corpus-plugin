# Corpus v2.0.0 发布核验

日期：2026-11-11。

本次以本地已部署的六工具候选为基础打包。corpus_core 的 25 个 Python 源文件与当前本地安装版逐文件字节一致；未更改业务逻辑或放开敏感动作。版本标识统一为 2.0.0，SQLite schema 仍为 v4。

## 本轮实际执行

- 源码：268 passed，0 failed，0 skipped。
- sdist 解包：268 passed，0 failed，0 skipped。
- wheel 解包：268 passed，0 failed，0 skipped。
- 每种形态额外 20 次真实进程中断恢复，共 60 次通过。
- Python 编译、构建、新 venv 离线 wheel 安装、CLI probe 及 wheel 内目录入口导入通过。
- direct 和 adapter 安装脚本在全新隔离目标安装通过；既有目标拒绝覆盖。
- 实际 Hermes PluginManager 在隔离 HOME 下加载发布 adapter，六工具注册与实际 handler 流程通过：提交、显式 runner、检索、原文回读、关联、草稿、回收以及敏感动作安全拒绝。
- 专用解释器 PDF 子进程执行，空白 PDF 如实返回 PARSE_EMPTY。
- 发布改动凭据模式扫描无匹配，git diff --check 通过。

以上测试在隔离目录执行，没有向正式资料库写测试资料。业务核心清单见 core-sha256.json。

## 不能混淆的历史证据

本轮发布测试离线，未新增计费 embedding/rerank 请求或重跑真实库迁移。此前精确候选及本地部署阶段已执行真实 DashScope 全链路、当前旧库副本迁移和回退；本次通过核心字节一致核对承接那些证据，不宣称重新执行。

本版仍未完成可信真人审批。正式报告发布、批量正式采用、合并、永久删除禁用；测试中的审批模拟不是真人授权。legacy_unknown 向量保留、external_unverified 原件及未复核关系，不代表全部接管。

## 发布范围

发布源码、sdist、wheel、插件 ZIP、SHA256 清单和说明。保留原仓库历史，旧 OpenClaw 代码与历史 CI 不作为 v2 六工具验收。未修改 GitHub Actions，因为当前令牌未包含 workflow 权限；不宣称 v2 远程 CI 通过。

本次发布不修改生产库、不重启网关，也不解锁敏感动作。升级请先阅读 DEPLOYMENT.md。
