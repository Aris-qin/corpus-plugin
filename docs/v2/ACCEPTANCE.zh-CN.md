# 候选修正验收报告（未部署）

日期：2026-11-10。先读 PARENT_CORRECTIONS，再读 EXECUTION_BOUNDARY；已阅读
独立审查与 JSON 复现结果。仍为批准的六工具范围，不是完整 M0–M6 验收声明。

## RED/GREEN 证据

新增正确行为断言的 `parent-corrections-red.log`：**15 failed**；新增核心集成
`bounded-integrations-red.log`：**7 failed, 1 passed**；预算/未来 DDL 加固的
`bounded-hardening-red.log`：**2 failed**。均在 `../evidence`，原独立审核未改动。
合并新增固定 URI 与原来源撤稿筛选的 `merge-impact-edge-red.log` 另有 **2 failed**。
不是把旧 bug 复现成功当验收通过；后加的已通过边界测试只算验证，不冒充 RED-first。

最终源码、sdist、wheel 全安全套件和编译见新 `../evidence/validation-*` 目录，
有 cwd、精确命令、退出码、schema/contracts 和包 SHA256。计数按最终实际日志，
不沿用旧 67 测试或把编译当业务测试。完整产品/宿主/生产验收仍未通过。

## 已实现修正

1. 合并后 canonical target、双方 context 均可发现两方来源；旧 version/chunk
   URI 保留，navigation/history 展开版本。禁止循环、alias 恢复和传递链；冻结
   实际 context/identifier/citation/link/review 影响指纹，变化需重提案。
2. 搜索/精确 version/chunk 返回元数据、出处、lifecycle、撤稿/勘误警示和核查。
3. 发布冻结来源 work revision/status 与 research revision/hash/current；变化
   返回 before/after 差异并拒绝旧审批。显式历史正式引用不被一刀切禁止。
4. 每次 runner/retry 及真正 parser 消费检查原件 hash+size；损坏失败关闭，
   旧 active generation/固定 URI 保留。blob 去重不丢 version-kind 等语义，
   显式 work 写入在去重分支也检查 expected_revision/lifecycle。
5. 元数据/assessment 严格类型、字段和界限；DOI 与 normalized identity 同事务
   更正、冲突拒绝；batch source/draft 复用顶层 schema，无宽泛替代写入口。
6. 核查跟随 resolved 固定 version/revision；chunk 包含 source/自身核查的 scope、
   日期和未决项。历史正式版标 superseded_formal，不进 drafts；overview 正式优先。
7. 检索过滤先于有界候选；正式/preferred 来源优先、带版本信息；旧证据可读。
   有显式 truncated、相关报告 current/drafts 导航、research body 独立查询和
   同 generation 邻接 chunk 展开。similar 有配置使用 semantic，否则明确词法降级。
8. 旧快照原样重放幂等；已有导入时拒绝任何不同快照，避免重复 lineage。
   稳定 project_slug、label/status 和已知关系映射，未解关系报告。
9. 包/后端/测试/validate 无硬编码开发路径；采集/构造前显式 sandbox root，
   本地 import 校验，路径逃逸拒绝，无生产默认回退。

## 核心集成的真实与模拟边界

- **真实 pypdf 子进程**提取两页合成 PDF；限 CPU/内存/文件/FD/时间，清空环境，
  worker 禁网并继承 parent 内核边界。原件/canonical/locator hash 一致；仅真实
  PDF page 有页码；图片/公式/OCR/bbox 未提取，明确警告。
- **真实 vec0 同 DDL 合成 fixture**：使用提供的实际 LEGACY_SCHEMA，可信安装包
  sqlite_vec 加载扩展。逻辑 vectors 与 shadow 状态都在 raw payload/备份；
  legacy_unknown 不与新 query 比较。未知虚表/未来 DDL 拒绝。没有生产快照测试。
- **真实 API 实现 + 离线 transport fixture**：显式 settings/data root/key env
  接通 submit→runner→search 同一 profile。持久批次 checkpoint、成功跳过、
  范围/预算预留、模糊计费失败停止调用、词法降级/incomplete job steps 已测试。
  没有授权/执行真实 embedding 服务，不把 fixture 当真实模型。
- **真实 SSRF 适配代码 + 离线获取 fixture**：类型/重定向/字节数有界并存 provenance；
  identifier 仅合法 DOI 确定性 resolver；登录/挑战拒绝，coverage 不冒称 fulltext。

## 精确剩余范围与门禁

1. 真实宿主 SDK/permission/可信 WebUI 人类审批未证实。CLI 绑定提案/hash/revision，
   但同 shell 模型也能调用，不能宣称不可伪造的人类身份；无 self-confirm boolean。
2. 无付费 embedding/provider 授权/真实调用。金额 ceiling 依赖 operator 保守
   每次价格；实际 token/价格/账单核对未做。模糊/中断批次无自动安全重试或账单
   裁定恢复流程，必须人工裁定；不得绕过门禁自动全库 reembed。ANN/规模验收未做。
3. 生产快照迁移/完整原件清点/恢复由 parent 单独执行，worker 未访问生产。
   vec0 合成同 DDL 不是生产迁移证明；不支持旧库增量同步，改快照需新 candidate。
4. OCR/公式/图表/bbox、复杂树分块、同层语义/reranker 未实现。pypdf 子进程
   不是独立跨平台安全沙箱认证，也不保证扫描 PDF 可提取文字。
5. 实际联网 TLS/站点兼容性/可靠书目匹配未验证。不自动研究，不保证付费墙、JS
   页面或任意 DOI 有可用全文；PMID 搜索和全文发现不支持。
6. 安全共享对象 GC/purge、未知外部引用穷举、Git 导出自动化未实现。
   purge fail closed，fsck 只报告；wiki/fact 只链接/通知，不读写正文。
7. 宿主通知、自动外源复核、完整期刊本体、可配置核查年龄阈值未完成；当前保留
   明确 scope/日期/remaining，并在正式用途反复提醒。

## 执行边界

代码、HOME/TMP、fixture、DB、包和证据都在授权构建根内；未收集旧测试，
未查看 credentials/system config，未访问生产/真实服务，未部署/改 auth/config，
未写 wiki/fact、commit 或 push。共享解释器 pyvenv.cfg 被内核拒绝后，使用
parent 提供的 sandbox-local validation-venv，未全局安装。
生产零写入依据是执行边界和命令，不是声称检查/取证过生产目录。
