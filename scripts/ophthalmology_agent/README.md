# Ophthalmology Intelligence Agent

优化版眼科信息情报代理：面向研究、监管、科普、趋势、行业动态五大维度，自动输出 NotebookLM 友好的结构化 Markdown 报告。

## 主要优化点

- 更稳健的网络请求：统一 `User-Agent` + 重试机制（降低公开 API/RSS 拒绝率）
- 数据质量控制：可信域名白名单过滤 + 双层去重（DOI/PMID/URL + 标题相似度）
- 结构化分析：自动分类、证据类型标注、关键结论、临床相关性、争议提示
- 趋势识别：新增热词信号统计（Hot Keyword Signals）
- 可扩展配置：关键词、数据源、运行频率、输出路径、Google Drive 同步均可在 `config.yaml` 管理

## 快速开始

```bash
python scripts/ophthalmology_agent/agent.py --config scripts/ophthalmology_agent/config.yaml
```

## 定时模式

```bash
# 每日 UTC 08:00
python scripts/ophthalmology_agent/agent.py --config scripts/ophthalmology_agent/config.yaml --schedule daily --at 08:00

# 每周一 UTC 08:00
python scripts/ophthalmology_agent/agent.py --config scripts/ophthalmology_agent/config.yaml --schedule weekly --weekday monday --at 08:00
```

## 输出文件

默认输出目录：`./data/ophthalmology_briefs`

- `ophthalmology_intelligence_brief_YYYY-MM-DD.md`
- `ophthalmology_intelligence_brief_YYYY-MM-DD_raw.json`

## Google Drive（可选）

1. 在 GCP 启用 Drive API，创建服务账号。
2. 下载服务账号 JSON。
3. 将目标 Drive 文件夹共享给服务账号邮箱。
4. 配置：
   - `google_drive.enabled: true`
   - `google_drive.service_account_file: /path/to/xxx.json`
   - `google_drive.folder_id: <folder_id>`

可选依赖：

```bash
pip install google-api-python-client google-auth
```

## 说明

- 代理不会生成无来源内容，所有条目保留原始链接。
- 自动摘要与证据标注基于规则引擎，建议最终用于临床决策前进行人工核验。
