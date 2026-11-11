# Corpus 本地替换与当前数据库迁移报告

日期：2026-11-11；状态：文件替换及基本检查完成，等待用户手动重启网关。未提交或推送 Git。

## 安装与数据路径

插件保持原注册名 corpus-query，位于 /home/hermeswebui/.hermes/plugins/corpus-query；q-hf 原有插件链接继续指向这里。default 与 q-hf 的配置通过 Hermes CLI 更新，均指向同一正式资料库。

新正式目录：/workspace/projects/_corpus/library-v1/
新数据库：/workspace/projects/_corpus/library-v1/data/catalogue.sqlite
非密钥设置：/workspace/projects/_corpus/library-v1/settings.json
旧数据库保留：/workspace/projects/_corpus/corpus.db
固定运行环境：/opt/data/corpus-runtime-v1/venv/

旧库未原位改写；本次从当前正式旧库创建 SQLite 一致性备份，迁入新 schema v4 后切换插件目标。迁移不是沿用历史测试库。旧库前后 SHA256 相同：b8da6dd1aefc0e65e17fd6658872047259c10e25805511f31becd75494593dcf。

## 迁移和独立检查

当前旧库的 418 份资料、42,548 个切片、42,498 条向量均已保留。14 张源表逐行核对 SQLite/Python 原始类型、字段、重复行计数，全部一致。新库 integrity_check=ok，foreign_key_check 无错误；fsck 返回 problems=[]，无删除操作。

default 与 q-hf 各用独立新进程，从实际插件安装目录自动发现，enabled=true、error=null、六工具注册。通过实际 handler 查询先前漏检的原文并回读成功，真实 DashScope 查询向量请求成功，rerank_complete。查询向量共一个成功批次，33 输入字节，没有全库重建。

六工具：corpus_ingest、corpus_search、corpus_read、corpus_reference、corpus_manage、corpus_jobs。

宿主 Hermes 当前缺少 pypdf/sqlite_vec，直接使用宿主解释器会造成能力缺失。本次未给宿主全局安装依赖，而新增薄宿主适配入口，把六工具调用路由至固定专用 venv；业务核心所有文件保持已验收清单字节一致。专用 venv 的 pypdf 7.0.1 和 sqlite_vec 来自之前实际测试通过的本地运行环境，实际 -I 子进程导入通过。

新增入口的隔离冒烟实际覆盖六工具、资料提交、显式 runner、完成状态、检索回读、草稿、关联、回收，以及敏感动作默认拒绝。PDF 子进程确实执行，空白 PDF 返回 PARSE_EMPTY，不伪造正文。没有向正式库写入合成测试资料，正式 works 仍为 418。

首次隔离脚本错误地对已被报告引用的来源期望生成删除提案，实际安全返回 PURGE_BLOCKED；修正测试预期，在新隔离目录重跑通过。此为测试预期错误，未修改业务核心或放宽删除保护。

本次没有重跑 268 项全套矩阵；此前精确候选已完成该矩阵，本轮对新增安装适配和当前正式迁移做部署基本检查，不把此前矩阵冒充本轮复跑。

## 当前限制：不可忽略

1. 42,498 条旧向量仍为 legacy_unknown（含历史孤立项），未认领模型来源，旧资料目前主要靠全局词法召回和真实 rerank；查询向量成功不代表旧库已经完成语义检索接管。
2. 418 份历史原件为 external_unverified，原文件没有移动或删除，也没有未经证据把它们宣布为托管原件；9,812 项历史关系仍待复核，15 份 self_written/未知来源仍隔离。
3. 正式发布、批量正式采用、合并、永久删除保持 SENSITIVE_ACTION_DISABLED；没有真实批准适配，不存在模型自确认开关。
4. 新资料可提交，但后台执行是显式 runner，不会随重启自动跑任务。运行入口为 /workspace/projects/_corpus/library-v1/corpus runner --max-jobs N，需要运行进程持有 QHF_DASHSCOPE_KEY 环境变量。
5. 向量计费权限保持严格的 operation_id 白名单。目前配置占位为 operator-approved-ingest-required，未经授权的新入库操作不会自动消费向量额度，会明确处于未完成状态。正式处理新文献前需给具体入库操作设置有界预算，再显式运行；未为方便使用而放开无限计费。
6. 本轮默认 embedding 预算最多 20 次、32,000 输入字节，估计上限 USD 0.20（每次 USD 0.01 为保守运维估值，不是供应商实际发票）；rerank 最多 20 次、200,000 字符。预算耗尽应明确降级，调整预算需显式操作。

## 配置与密钥

只改动 default/q-hf 配置的 plugins 顶层子树，其余配置核对未变。将 q-hf 已有的 QHF_DASHSCOPE_KEY 安全同步至根 .env，使重启后的 default 网关能继承；未在报告、JSON 或代码中写入密钥。没有改动其他 profile 的插件、技能或记忆。

模型维持 text-embedding-v4@1024；rerank 为 qwen3-rerank，使用实测成功的完整 DashScope 原生 endpoint，未替换模型或密钥。

## 备份、回退与重启

完整备份：/home/hermeswebui/.hermes/backups/20261111T104817Z-corpus-deploy/
其中包括旧插件、default/q-hf 配置及 .env、当前旧库一致性副本、restore.sh。目录权限 700，包含秘密文件，不应分享或提交 Git。

回退脚本仅恢复配置及插件，不自动重启；新资料库保留待查，不会直接删除。回退应先停止相关 corpus 操作，避免新旧入口同时写入。旧库一直原样保留。

网关 PID 7 未变，没有重载或重启。用户可现在手动重启网关，随后新建 session；重启后仍需核实实际运行会话暴露六工具及密钥继承。当前对话不会强行更换已有工具接口。

执行证据：/opt/data/corpus-deploy-20261111T104817Z/，含当前迁移逐字段核对、两 profile 安装读取、适配入口测试及最终状态检查。
