# 自动文献解读流水线

正式入口是 `collector.py`，GitHub Actions 入口是 `../.github/workflows/daily-literature.yml`。

## 每日流程

```text
Europe PMC 检索
  → 衰老/二代/三代/多组学/工具相关性评分
  → 每次最多选择 5 篇新候选
  → 获取 PMC 全文 XML、Methods、Results 和 Figure 图注
  → 从 PMC 文章 AWS 数据集解析原始图文件，下载并校验原图，保存到 web/generated/assets/<PMCID>/
  → EasyScholar 查询 IF/JCR
  → 仅保留 EasyScholar 确认的 SCI/JCR 1 区期刊
  → DeepSeek 生成结构化中文公众号式解读
  → 正文、图注、原图、字段完整性质量门禁
  → 追加 web/generated/articles.json 并部署 Pages
```

## 安全的发布条件

只有同时满足以下条件的论文才会进入公开网页：

1. 主题评分达到阈值；
2. 正文 XML 可以读取；
3. 至少有一张带原始图注的正文 Figure；图片先下载到网站资源目录并校验为真实图像，不截图、不裁剪；公开网页只使用已验证的本地原图，远程直链不能替代本地资源；
4. AI 返回完整的导语、背景、研究问题、方法、结果、局限、启示和逐图解读；
5. 通过质量门禁。

其中第 5 项还包含严格的 JCR/SCI 1 区门禁：没有 EasyScholar 密钥、没有匹配到期刊指标、或返回的分区不是 Q1/1 区时，候选不会调用 DeepSeek，也不会写入公开文章库。IF 数值本身不替代分区判断。相关性硬门槛还要求题目/摘要出现明确的测序、组学平台或组学数据分析信号；仅提到一次 single-cell/RNA、pipeline、workflow 或 bioinformatics 的非测序论文不会入选。

没有正文或原图的候选会写入 `web/generated/run-report.json`，状态为 `discarded`，不会污染网站。PMC 在 2026 年迁移了文章数据分发地址，旧的 `pmc.ncbi.nlm.nih.gov/articles/.../bin/...` 图链接可能返回 404；程序现在会查询官方 `pmc-oa-opendata` AWS 清单并使用同名原图。若文章本身没有可再分发的图文件，则仍会丢弃该候选。全文完整但 AI 暂时失败的候选状态为 `manual_review`，最多有限重试；已有 `published` 文章不会因为一次接口失败被清空。

Europe PMC 的临时 5xx/429/超时会自动重试；若重试后仍不可用，本轮会以 `upstream_unavailable` 写入运行报告并保留原有文章，工作流不会因一次上游故障清空网站或直接失败。待接口恢复后，下一次定时运行会从未处理候选继续检索。

如果旧版本文章只有 PMC 远程原图、尚未有本地缓存，升级后的流程会先尝试补存本地图；补存失败或无法验证的记录会从公开文章库移除并在运行报告中标记，避免网页留下无图文章。若历史运行曾把状态误写为空，Actions 会从 Git 历史中恢复最近一个非空快照，再由当前的 JCR、全文、原图和 AI 门禁重新核验。

AI 失败原因会写入 `processed[].ai_status` 和 `processed[].ai_error`，例如 `llm_http_401`、`llm_api_payload:quota exceeded`、`llm_error:empty_content` 或 `llm_schema_incomplete:methods,results`。这比只看到“0 篇”更适合定位密钥、额度、代理返回格式和上下文过长问题。生成文本会统一转义换行并清除成对 Markdown 标记；图注只修复 XML 内嵌标签造成的括号、标点和连字符空格，不改数字、单位和统计显著性符号。

## 运行方式

本地用 fixture 测试：

```bash
python3 -m unittest automation.test_collector -v
python3 automation/collector.py \
  --fixture automation/fixtures/europepmc.json \
  --xml-fixture automation/fixtures/pmc-example.xml \
  --assets-dir /tmp/library-assets \
  --state /tmp/library-state.json \
  --output /tmp/library-articles.json \
  --report /tmp/library-report.json
```

真实运行不传 `--fixture`，会请求 Europe PMC。默认每天最多新增 5 篇、检查 40 个候选。网页入口为 `web/auto-library.html`；现有 `web/literature-demo.html` 的三篇人工核验示范不会被覆盖。

## 数据和指标

`data/automation-state.json` 保存稳定 ID、已发布记录、有限重试状态和 EasyScholar 缓存；`web/generated/articles.json` 只包含已通过门禁的公开文章；`web/generated/run-report.json` 保存本轮摘要和失败原因；`web/generated/assets/` 保存已发布文章的原始正文图。运行时下载到临时目录的文件可以删除，但已发布网页使用的本地资源不能删除。

作者和期刊在数据中分开保存。网页展示为：

```text
作者：Author A、Author B
期刊：Nature Aging（IF 2024：…；JCR 2024：Q1）
```

EasyScholar 没有匹配结果时，候选会被丢弃并在运行报告中注明“期刊未确认属于 JCR/SCI 1 区”，不由模型猜测。接口没有返回指标年份时也不伪造年份。
