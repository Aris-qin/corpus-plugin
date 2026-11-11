# v2 安装与迁移边界

## 安装入口

root __init__.py 为直接 Hermes 插件入口，适用于宿主解释器已具备相应依赖的情况。plugins/hermes-corpus-query 是专用解释器适配入口，包含相同六工具契约和业务核心；它不会发现或读取默认生产目录。旧 plugins/corpus-query 是 OpenClaw 实现，不要用于本次 Hermes 部署。

install.sh 必须指定绝对 --target，目标已存在时拒绝覆盖。不修改 config.yaml、不装依赖、不迁移业务数据、不重启。默认 mode=direct；mode=adapter 安装固定解释器桥接入口。

## adapter 配置

通过 Hermes 配置工具，在 plugins.entries.corpus-query.settings 中设置以下字段（所有路径必须替换为实际路径，不含密钥）：

```json
{
  "authorized_root": "/ABSOLUTE/authorized",
  "settings_file": "/ABSOLUTE/authorized/settings.json",
  "runtime_python": "/ABSOLUTE/runtime/venv/bin/python",
  "runtime_root": "/ABSOLUTE/authorized/corpus-plugin"
}
```

runtime_root 下包含 corpus_core。专用解释器需安装 pypdf（PDF 文本）及 sqlite-vec（旧 vec0 迁移）。核心要求 Python >=3.11 和 SQLite FTS5。依赖未安装时对应能力失败，不伪造正文或向量。

非密钥 settings.json 的严格字段及预算示例见 IMPLEMENTATION-DETAILS.md。调用 provider 前将实际 key 置于命名环境变量；不要写入 JSON、发布包或日志。已验证的现用组合是 text-embedding-v4@1024 和 qwen3-rerank 的 DashScope 原生协议，不意味着其他模型自动兼容。

## 迁移与回退

1. 停止入库写操作，备份旧插件、配置和当前 SQLite 一致性副本。
2. 在新的数据根使用 migrate-readonly 导入，不原地改写旧库。先阅读 corpus CLI --help 确认授权根、settings 与 source 参数。
3. 核对原表字段、向量字节、引用、完整性及待复核清单。保真不等于托管原件或已知向量来源。
4. 实际宿主加载后执行有界检索与原文回读；新入库测试使用独立测试库，不拿真实资料测试删除。
5. 验证后切换显式 settings；加载与重启由操作者执行。回退时先停新入口，恢复插件与配置，保留新库待查，不自动删除。

不得把旧向量自行标为已知模型，或在缺乏历史证据时以同名文件接管原件。原库与历史快照需要单独备份策略；永久删除仍安全禁用。

## 执行任务与预算

入库提交不等于处理完成。通过 corpus runner --max-jobs N 显式运行，并限制具体 operation_id、调用次数、输入量与成本上限。预算不足显示 partial；仅恢复安全未完成阶段。未知计费结果禁止盲目重试。

部署环境的累计预算会被查询消耗，生产持续使用前应由操作者检查预算，而不是无限放开。重启不是自动 runner，也不会自动认领 legacy_unknown。

## 验收范围

此前本地实际部署完成 schema v4 迁移、六工具安装适配、真实 DashScope 检索和原文读取。原始证据包含本地数据盘点，不将全部生产明细复制到公共仓库。发布版业务核心与已验收候选逐文件哈希一致；版本标识、文档与安装/打包验证另行核验。
