# 自动文献解读流水线

正式入口是 `collector.py`，GitHub Actions 入口是 `../.github/workflows/daily-literature.yml`。

## 每日流程

```text
Europe PMC 检索
  → 衰老/二代/三代/多组学/工具相关性评分
  → 每次最多选择 5 篇新候选
  → 获取 PMC 全文 XML、Methods、Results 和 Figure 原图/图注
  → EasyScholar 查询 IF/JCR
  → DeepSeek 生成结构化中文公众号式解读
  → 正文、图注、原图、字段完整性质量门禁
  → 追加 web/generated/articles.json 并部署 Pages
```

## 安全的发布条件

只有同时满足以下条件的论文才会进入公开网页：

1. 主题评分达到阈值；
2. 正文 XML 可以读取；
3. 至少有一张带原始图注的正文 Figure，图片使用论文/PMC 原图直链，不截图、不裁剪；
4. AI 返回完整的导语、背景、研究问题、方法、结果、局限、启示和逐图解读；
5. 通过质量门禁。

没有正文或原图的候选会写入 `web/generated/run-report.json`，状态为 `discarded`，不会污染网站。全文完整但 AI 暂时失败的候选状态为 `manual_review`，最多有限重试；已有 `published` 文章不会因为一次接口失败被清空。

## 运行方式

本地用 fixture 测试：

```bash
python3 -m unittest automation.test_collector -v
python3 automation/collector.py \
  --fixture automation/fixtures/europepmc.json \
  --xml-fixture automation/fixtures/pmc-example.xml \
  --state /tmp/library-state.json \
  --output /tmp/library-articles.json \
  --report /tmp/library-report.json
```

真实运行不传 `--fixture`，会请求 Europe PMC。默认每天最多新增 5 篇、检查 40 个候选。网页入口为 `web/auto-library.html`；现有 `web/literature-demo.html` 的三篇人工核验示范不会被覆盖。

## 数据和指标

`data/automation-state.json` 保存稳定 ID、已发布记录、有限重试状态和 EasyScholar 缓存；`web/generated/articles.json` 只包含已通过门禁的公开文章；`web/generated/run-report.json` 保存本轮摘要和失败原因。

作者和期刊在数据中分开保存。网页展示为：

```text
作者：Author A、Author B
期刊：Nature Aging（IF 2024：…；JCR 2024：Q1）
```

EasyScholar 没有匹配结果时显示“指标待查”，不由模型猜测。接口没有返回指标年份时也不伪造年份。
