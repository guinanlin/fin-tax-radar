# tax-radar1

牛角包财税资讯 - 财税资讯聚合平台（静态站点 + 定时采集）。

当前版本以 **官方站点资讯** 为主，保留 **B站** 作为补充来源，并按四类业务分类展示：

- 政策法规（policy）
- 税务动态（tax）
- 财务实务（finance）
- 宏观财经（macro）

---

## 1. 项目结构

- `collect_data.py`：数据采集脚本（Python）
- `index.html`：前端页面（GitHub Pages 可直接托管）
- `data/`：采集结果目录（JSON）
  - `data/policy/latest.json`
  - `data/tax/latest.json`
  - `data/finance/latest.json`
  - `data/macro/latest.json`
- `.github/workflows/collect.yml`：GitHub Actions 定时采集任务

---

## 2. 数据来源

### 官方站点（主来源）

- 国家税务总局（`chinatax.gov.cn`）
- 财政部（`mof.gov.cn`）
- 12366纳税服务（`12366.chinatax.gov.cn`）
- 税屋（`shui5.cn`）
- 中国会计视野（`esnai.com`）
- 中国税务网（`ctaxnews.com.cn`）
- 巨潮资讯网（`cninfo.com.cn`）
- 国家法律法规数据库（`flk.npc.gov.cn`）

### 视频来源（保留）

- B站（`bilibili.com`）

---

## 3. 分类规则

分类在 `collect_data.py` 的 `CATEGORY_CONFIG` 中配置：

- `policy`：政策法规
- `tax`：税务动态
- `finance`：财务实务
- `macro`：宏观财经

每个分类配置包含：

- `search_terms`：检索词
- `filter_keywords` / `filter_phrases`：内容相关性过滤
- `site_domains`：该分类优先站点

---

## 4. 采集与排序逻辑

1. **官方站点采集**  
   通过站点限定检索（RSS）拉取候选内容，再做关键词过滤与时间解析。

2. **B站采集**  
   使用公开搜索接口（按发布时间）+ 排行接口补充内容。

3. **去重**  
   按标题归一化去重，重复标题优先保留较新的内容。

4. **时效过滤**  
   按分类最大时效过滤（可在 `CATEGORY_MAX_AGE_DAYS` 调整）。

5. **来源平衡**  
   为避免 B站结果过多，应用来源平衡策略，确保官方站点内容有足够展示占比。

6. **输出**  
   每类输出到 `data/<category>/latest.json`。

---

## 5. 本地运行

### 环境要求

- Python 3.10+
- 依赖：`httpx`、`beautifulsoup4`、`lxml`

### 安装依赖

```bash
pip install httpx beautifulsoup4 lxml
```

### 执行采集

```bash
python collect_data.py
```

执行后会更新 `data/` 目录中的四个 `latest.json` 文件。

---

## 6. 前端展示

`index.html` 会优先读取 `data/` 下实时 JSON 数据；若读取失败则回退到内置 mock 数据。

前端支持：

- 四分类切换
- 来源筛选
- 关键词搜索
- 热度/时间排序
- 详情弹窗与复制

---

## 7. GitHub Actions 自动采集

工作流文件：`.github/workflows/collect.yml`

默认每天两次运行（UTC）：

- `0 0 * * *`（北京时间 08:00）
- `0 10 * * *`（北京时间 18:00）

流程：

1. 安装 Python 和依赖
2. 运行 `python collect_data.py`
3. 提交并推送 `data/` 变更

---

## 8. 常见问题

### Q1: 为什么某次只有 B站数据？

可能原因：

- 官方站点检索当次返回少
- 网络环境对搜索引擎结果有波动
- 当前关键词在指定站点下命中较少

建议：

- 调整 `CATEGORY_CONFIG` 的 `search_terms`
- 扩充 `site_domains`
- 适当放宽 `filter_keywords` / `filter_phrases`

### Q2: 如何新增分类？

需要同时修改：

- `collect_data.py`：`CATEGORY_CONFIG`、`CATEGORY_MAX_AGE_DAYS`、输出 `file_map`
- `index.html`：tab、实时数据加载映射、分类文案

---

## 9. 后续优化建议

- 对每个官方站点做定制化解析器（替代通用检索）
- 引入 Redis/文件缓存，降低重复抓取成本
- 增加抓取质量评分（权威度、时效、主题一致性）
- 增加失败重试与告警（邮件/飞书/钉钉）
