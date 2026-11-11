# corpus-plugin v2.0.0

Corpus 是全局共享的研究资料与检索库，保存原始文献和原文位置，管理期刊画像、作者指引、专题调研及项目参考档案。项目与主题组织引用，不分割原始资料库。

Corpus 不替代 researcher 调研，不复制 wiki 编译正文，不接管 fact 执行状态。

## 六工具

| 工具 | 职责 |
|---|---|
| corpus_ingest | 资料提交、去重与入库 |
| corpus_search | 全局证据、调研和导航检索 |
| corpus_read | 原文、切片上下文、版本及固定引用 |
| corpus_reference | 项目参考档案、期刊画像和专题报告草稿 |
| corpus_manage | 元数据、关联、复核、回收与维护 |
| corpus_jobs | 任务查看与安全恢复 |

## v2 变化

全局模型分离文献身份、内容版本、原件、处理代和切片。调研资料保留当前版与历史修订，使用时提醒时效。SQLite schema v4 支持旧库只读迁移及原始字段保留。

分阶段入库支持 partial 状态和安全重试，不重复已完成向量请求。全局召回在评分前不再任意截断前 10,000 个切片，采用有界工作集扫描并披露成本，不宣称 ANN。支持显式配置的 embedding 与 rerank，包括 DashScope 原生协议。

## 安全与运维限制

- 正式报告发布、批量正式采用、合并、永久删除默认 SENSITIVE_ACTION_DISABLED。没有可信真人审批适配，模型不能自确认解锁。草稿、检索、读取、关联、回收和恢复可用。
- 旧向量 legacy_unknown 不能直接与新查询向量混用；保真迁移不等于完成语义接管。不自动全库重建。
- 历史原件 external_unverified 和未解析关系需要显式接管，不凭同名文件猜测。
- 入库提交后必须显式 runner；计费操作需要具体 operation_id 白名单及累计预算，不自动后台索引。
- PDF 文本依赖 pypdf，旧 vec0 迁移依赖 sqlite-vec；OCR、复杂图表、公式及层级切片未完整验收。
- 原 corpus/、OpenClaw plugins/corpus-query 和 fact-infra 保留为历史实现，不是 v2 默认入口，未纳入本次重验。

## 安装

要求 Python >=3.11 和 SQLite FTS5，推荐专用 venv。宿主有依赖时使用 direct；宿主缺依赖时使用 adapter。安装脚本拒绝覆盖，不修改配置、数据库或重启。

```sh
python3 -m venv /ABSOLUTE/runtime/venv
/ABSOLUTE/runtime/venv/bin/python -m pip install pypdf sqlite-vec
bash install.sh --target /ABSOLUTE/hermes-home/plugins/corpus-query --mode direct
# 或采用专用解释器适配入口：
bash install.sh --target /ABSOLUTE/hermes-home/plugins/corpus-query --mode adapter
```

adapter 需明确 authorized_root、settings_file、runtime_python、runtime_root 四个绝对路径。direct 使用 CORPUS_AUTHORIZED_SANDBOX_ROOT 及 CORPUS_SETTINGS_FILE。数据与非密钥设置必须位于授权根内，密钥仅由环境变量注入。

分发包暂保留兼容名 corpus-candidate，版本统一为 2.0.0；Python 模块 corpus_core，运行插件名 corpus-query。wheel 内 corpus-candidate 数据目录是兼容路径，不是旧版本。

从源码根目录执行 CLI，或先安装 wheel：

```sh
export CORPUS_AUTHORIZED_SANDBOX_ROOT=/ABSOLUTE/authorized
export CORPUS_SETTINGS_FILE=/ABSOLUTE/authorized/settings.json
/ABSOLUTE/runtime/venv/bin/python -m corpus_core.cli --settings "$CORPUS_SETTINGS_FILE" probe
```

完整安装、settings、迁移及回退见 [DEPLOYMENT.md](docs/v2/DEPLOYMENT.md)。源码运行环境需位于明确授权根内。升级先备份插件、配置与数据库，在新数据根只读迁移、核对后切换，不直接覆盖旧库。

## 验证

```sh
bash tools/validate.sh --sandbox-root /ABSOLUTE/authorized --python /ABSOLUTE/validation-venv/bin/python
```

验证环境需 pytest、pypdf、sqlite-vec，源码必须位于 sandbox 内。脚本分别测试源码、sdist、wheel、编译及进程中断恢复，隔离 HOME/TMP。发布核验结果见 [RELEASE-VALIDATION.md](docs/v2/RELEASE-VALIDATION.md)。原 GitHub Actions 面向历史 v1，不代表 v2 六工具验证；本次以本地独立发布矩阵为依据。

## 文档

- [产品方案](docs/v2/product-plan-20261110.md)
- [技术方案](docs/v2/tech-plan-v1-20261110.md)
- [动作契约](docs/v2/ACTION_CONTRACTS.md)
- [实现细节](docs/v2/IMPLEMENTATION-DETAILS.md)：保留候选阶段历史说明，发布状态以本 README 为准。
- [安装与迁移](docs/v2/DEPLOYMENT.md)
- [变更记录](CHANGELOG.md)

MIT License。
