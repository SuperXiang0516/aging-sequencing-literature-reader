# 论文绘图案例库

独立于「衰老测序文献雷达」的网站原型。独立页面、数据、运行状态与未来部署；不读取、修改或依赖原日报的业务数据。

## 当前已完成

- `web/index.html`：自动更新网站入口；以公众号/Word 式连续文章展示 `web/generated/articles.json` 中通过门禁的论文。
- 「图形形式」与「分析功能」两个独立下拉选项，支持组合筛选。
- 论文目录、图源与代码链接、输入文件结构、中文静态阅读笔记、待核问题。
- 已移除双入口导航和衰老日报快照。

当前仓库已经包含一个独立的自动化入口：`automation/collector.py`、`.github/workflows/daily-literature.yml` 和 `web/auto-library.html`。它不会执行作者代码，也不会把 PDF 页面截图当作正文图；它只在全文、正文图和图注都能追溯时发布文章。现有 `literature-demo.html` 的 3 篇示范仍然保留。

自动化流程为：Europe PMC 候选检索 → 衰老/二代/三代/多组学/工具开发相关性评分 → 只选取尚未处理且分数达标的候选（默认每天最多 5 篇）→ 获取 PMC 全文 XML、正文方法、结果和 Figure 原图直链 → 查询 EasyScholar 期刊指标 → 只保留 EasyScholar 确认的 SCI/JCR 1 区期刊 → 调用 DeepSeek/OpenAI 兼容接口生成公众号式中文解读 → 质量门禁 → 写入 `web/generated/articles.json` → GitHub Pages 发布。无法获取正文、正文图、图注或 Q1 指标的候选会进入运行报告，不会显示在网站上；暂时的 AI 失败会在后续运行中有限重试。

## 后续复用范围

从原项目提取或复制经检查的通用客户端和缓存逻辑，保留适用的许可证与来源说明：

- NCBI 文献信息请求、限速、超时与重试。
- DeepSeek / OpenAI 兼容文本请求、结构化输出检查与失败重试。
- easyScholar 期刊指标查询、字段校验与缓存机制。

不直接复用原项目的衰老检索条件、摘要模板、业务输出目录或日报构建入口。新站自己的候选池、绘图记录、失败队列和期刊缓存由新站维护；共用缓存实现不等于两个仓库同时写一个缓存文件。初期优先保持代码版本固定，不在线拉取另一个仓库的主分支代码执行。

## API 与安全

上线时可继续使用已有 API 账户与密钥，但新 GitHub 仓库需要单独配置 Secrets；现有仓库的 repository secrets 不会因为复制代码而自动转移。

拟沿用的名称：

- `LLM_API_URL`
- `LLM_API_KEY`
- `LLM_MODEL`
- `NCBI_EMAIL`
- `NCBI_API_KEY`
- `EASYSCHOLAR_SECRET_KEY`

所有密钥仅用于未来 GitHub Actions 后端，绝不写入前端、JSON 数据、日志或 Git 记录。不要把密钥发到聊天或 Issue。同一账户的两个网站会合计消耗供应商额度和费用；应错开请求时段并设置各自预算，不能假设两份独立限速就满足账户总限额。

GitHub Secrets 官方说明：https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets

## 自动化入口与密钥

网站入口是 `web/index.html`（源模板为 `web/auto-library.html`）。GitHub Actions 每天 01:17 UTC（北京时间 09:17）运行，也可以在 Actions 页面手动运行。页面采用本地测试的公众号/Word 式详情页：文章信息 → 背景与问题 → 研究设计 → 完整方法翻译 → 结果解析 → 正文原图与逐图图注/解读 → 局限与启发。作者和期刊分开显示，期刊格式为 `期刊名（IF …；JCR …）`；未被 EasyScholar 确认 SCI/JCR 1 区的文章不会发布。

需要在 GitHub 仓库的 Settings → Secrets and variables → Actions 中添加：

| Secret | 用途 |
| --- | --- |
| `LLM_API_KEY` 或 `DEEPSEEK_API_KEY` | 生成中文方法/结果/图注解读 |
| `LLM_API_URL` | 可选；不填时使用 DeepSeek Chat Completions 地址 |
| `LLM_MODEL` | 可选；不填时使用 `deepseek-chat` |
| `EASYSCHOLAR_SECRET_KEY` | 必需；查询 IF/JCR，只有确认 SCI/JCR 1 区的期刊才会发布 |

不要把密钥写进 JSON、HTML、脚本或提交记录。`NCBI_API_KEY` 不是此入口的必需项，因为候选检索默认使用 Europe PMC；后续增加 NCBI 备用源时再配置即可。

## 独立上线计划

1. 确认独立页面的布局与案例详情结构。
2. 新建独立 GitHub 仓库（建议名 `paper-plot-library`，尚未创建），仅上传此项目及后续需要的文件，不上传整个研究工作目录。
3. 接入通用 API 客户端及独立绘图收集流程；每日最多入库 5 篇，资料不足时不凑数。
4. 完成许可、图—脚本映射、输入结构和失败恢复检查后，配置独立 Pages 与 Actions。

现有衰老网站继续使用自己的仓库、网址和任务；本原型不会改变它。
