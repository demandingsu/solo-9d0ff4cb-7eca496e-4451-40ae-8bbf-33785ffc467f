# 线上展馆 API：参观路径与目录筛选

从浏览器上报的 `open` / `close` 事件还原匿名观众的实际参观路径，避免把未关闭页面
计作完整参观；支持乱序补报、重传去重与数据同意撤回。另提供带版本目录的展品录入、
更正/下架，以及按材质/主题标签的目录筛选与“逐个标签切换后的命中件数”预估。

技术栈：Python 3.11 + FastAPI + SQLite（WAL 模式），Docker Compose 一键启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档：
#   Swagger UI  http://localhost:8000/docs
#   ReDoc       http://localhost:8000/redoc
# 健康检查     http://localhost:8000/health
```

SQLite 数据文件存放在命名卷 `exhibition-data`（容器内 `/data/exhibition.db`），
容器重建不丢数据。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
EXHIBITION_DB=./exhibition.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events` | 上报单个事件 |
| POST | `/events/batch` | 批量上报，单事务原子提交 |
| GET | `/visitors/{visitor_id}/path` | 查询观众参观路径 |
| POST | `/visitors/{visitor_id}/withdraw` | 撤回数据使用同意（body 可省略，或传 `{"visitor_id": "..."}`） |
| POST | `/catalog/entries` | 录入展品（展品号 + 材质/主题标签 + 在展状态） |
| PATCH | `/catalog/entries/{exhibit_id}` | 更正标签 / 改状态；`status=offline` 即下架 |
| DELETE | `/catalog/entries/{exhibit_id}` | 下架（软下架，等价于 `status=offline`） |
| GET | `/catalog/filter` | 标签筛选（查询参数形式，与 POST 等价） |
| POST | `/catalog/filter` | 标签筛选，返回命中展品、目录版本与逐个标签切换后的件数 |
| GET | `/health` | 健康检查 |

事件字段：`event_id`（全局唯一，重传复用同一号）、`visitor_id`（匿名观众号）、
`page_view_id`（页面浏览号，同一次浏览的开关共享）、`exhibit_id`（展品号）、
`event_type`（`open` / `close`）、`event_time`（**带时区的 UTC ISO-8601**，
如 `2026-11-18T08:00:00Z`；无时区时间戳返回 400）。

### 上报响应语义

- `stored`：首次入库
- `duplicate`：`event_id` 已存在，幂等忽略（即使重传内容被篡改，也以首次内容为准）
- `rejected_withdrawn`：该观众已撤回同意，事件不入库，路径不会因此恢复

HTTP 状态码：成功受理固定 `201`，具体结果看响应体 `status`；参数/时间非法为
`422` / `400`。

### 路径响应

按 `page_view_id` 配对开关，同一观众按 **open 时间** 排序（`sequence` 从 1 连续编号）：

- 有关闭：`dwell_known=true`，`dwell_seconds` 为停留秒数
- 缺关闭：仍计为一次参观，但 `closed_at=null`、`dwell_known=false`、`dwell_seconds=null`（停留未知）
- 只有 close 没有 open 的孤儿事件不产生参观项
- close 早于 open 的异常数据：保留关闭时间，但停留时长标记未知

## 目录筛选

### 录入与维护

`POST /catalog/entries`：`{"exhibit_id": "EX-1", "materials": ["木", "青铜"],
"themes": ["古代文明"], "status": "online"}`。标签会去空白、去重、排序；
`exhibit_id` 已存在返回 `409`（应改用更正接口）。

`PATCH /catalog/entries/{exhibit_id}`：只修改提交的字段，`materials` / `themes`
整体替换，未提交的字段保持原值：

```json
{"themes": ["现代艺术"]}        // 更正主题标签
{"status": "offline"}           // 下架；标签保留
{"status": "online"}            // 重新在展
```

- `DELETE /catalog/entries/{exhibit_id}` 等价于 `{"status": "offline"}` 的软下架：
  保留标签与历史修订，之后可再设回 `online`。
- 与现状态完全相同的提交是幂等空操作：返回 `changed=false`，不产生新版本。
- 展品不存在 → `404`；字段非法 / 空 patch → `422`；任何失败都回滚整个写事务，
  原目录与已发出的预估都不受影响。
- `status=offline`（下架）的展品不计入任何命中；只要成功后目录就前进一个版本。

### 筛选与切换预估

`POST /catalog/filter`（`GET /catalog/filter?materials=木&materials=纸&themes=古代文明`
等价，方便在 `/docs` 里直接点选）：

```json
{"materials": ["木", "纸"], "themes": ["古代文明"], "catalog_version": 12}
```

- **同类多选命中任一项**（`materials` 内 OR、`themes` 内 OR）；
  **两类都选须同时满足**（材质 AND 主题）；**空选的类不限**。
- `catalog_version` 可省略：省略时使用现行目录；做“勾上/取消一个标签”的预估时，
  请沿用上一次响应里的 `catalog_version`。
- 返回：
  - `matched_exhibits`：命中的在展展品，**每个展品只出现一次**，按展品号排序；
  - `matched_count`：命中件数；
  - `catalog_version` / `is_current_version`：本次实际查询的目录版本，以及它是否现行；
  - `material_facets` / `theme_facets`：该版本下出现的每个标签，
    `selected` 表示本次是否已勾选，`count_if_toggled` 表示**单独切换这个标签**
    （其余选择保持不变）后的命中件数。
- 预估与命中严格同版本：即使期间有人改了标签或下架，按同一
  `catalog_version` 重查得到的命中件数与预估完全一致；不传版本的新查询则跟随现行目录。
- 不存在的版本返回 `404`；空目录的现行版本为 `0`，筛出 0 件。

## 设计要点（如何满足需求）

1. **重传不重复计数**：`events.event_id` 为主键；插入前先查存在性，存在即返回
   `duplicate`，整段逻辑在同一事务内。
2. **乱序补报照常处理**：路径不做物化存储，每次查询从已提交事件实时配对、排序。
   后补的较早 open 会在下次查询时自然改变路径顺序；后补的 close 会把“停留未知”
   变成已知时长。
3. **撤回后不可恢复**：`withdrawals` 表持久化撤回标记；撤回在一个 `BEGIN IMMEDIATE`
   事务内完成「删除该观众全部事件 + 写撤回标记」。此后任何上传（包括旧事件重放）
   在同一事务内先查撤回标记，一律 `rejected_withdrawn`。
4. **交错操作基于同一已提交状态、不露半份路径**：
   - SQLite 开 WAL，写请求统一 `BEGIN IMMEDIATE` 立即拿写锁，配合 `busy_timeout`
     串行化所有写事务；
   - 读请求在单个 `BEGIN DEFERRED` 快照事务里完成「撤回检查 + 事件扫描」，
     要么看到撤回前的完整状态，要么看到撤回后的空路径；
   - 路径是查询时推导的，不存在“路径表更新一半”的中间态；
   - 批量上报为单事务，任一事件非法则整批回滚。
5. **线程/进程并发**：每请求一个独立 SQLite 连接（`check_same_thread=False`
   以适配 uvicorn 线程池，连接不在请求间共享）；多个 uvicorn worker 共享同一
   数据卷时由 SQLite 文件锁保证安全。
6. **目录版本化但不复制目录**：`catalog_entries` 按“修订”存行——每次成功编辑只把
   该展品上一条现行行关闭（`valid_to = 新版本`）并插入一条新行
   （`valid_from = 新版本`），版本 V 用半开区间
   `valid_from <= V AND (valid_to IS NULL OR valid_to > V)` 读取。历史版本无需
   整目录快照，写入量恒为 1 行，按版本预估天然稳定。
7. **一次扫描得出全部切换件数**：筛选请求在只读快照事务中只读一次目录行，
   命中集与每个标签的切换件数都在这趟扫描里累计（选中标签按“唯一命中”扣减，
   未选标签按“当前未通过该类且带此标签”增加；取消该类最后一个标签即解除该类
   限制）。标签再多也不重扫目录，更不复制目录。
8. **编辑与查询同一已提交状态**：录入/更正走 `BEGIN IMMEDIATE`，版本号推进与
   修订写入同事务；筛选走单个 `BEGIN DEFERRED` 快照。并发改稿时，一次筛选
   要么全看到改前、要么全看到改后，且响应里的版本可被稳定重查。

## 目录结构

```
app/
  config.py    环境变量配置（DB 路径、busy timeout）
  db.py        连接、schema、WAL/pragma、事务管理
  models.py    Pydantic 请求/响应模型
  crud.py      领域逻辑：入库去重、开关配对、路径推导、撤回、目录版本与筛选
  main.py      FastAPI 路由与生命周期
tests/
  conftest.py
  test_api.py      15 项路径/撤回/并发测试
  test_catalog.py  19 项目录录入、更正/下架、多选筛选、切换预估、版本固定、并发测试
  stress_http.py   针对运行中服务的真实 HTTP 并发压测（手动运行）
Dockerfile
docker-compose.yml
```

## 自测

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

覆盖：目录录入与标签规范化、同类 OR/跨类 AND/空类不限、逐个标签切换件数
与真实重查一致（含两类多选、重复命中只计一次）、改稿/下架后固定版本预估不变、
不传版本时跟随现行目录、失败编辑保留原目录、并发改稿与筛选不混版本、
单次扫描与“每次编辑只写一行”的存储校验；以及路径部分——正常配对与停留时长、乱序（close 先到）与更早事件补报导致的重排、
缺关闭标记未知及补关后转已知、孤儿 close、`event_id` 重传（含篡改内容）、
批量去重与整批原子回滚、撤回后路径消失/重复撤回幂等/旧事件重放与新事件均被拒、
多线程上传-查询-撤回交错（进程内 TestClient 与真实 HTTP 两种）、
无时区时间拒绝、时区偏移归一化、负停留异常、WAL 与主键的存储层校验。

## 已知限制 / 未完成项

- 本开发沙箱没有 Docker 守护进程，`Dockerfile` 与 `docker-compose.yml` 沿用原有
  配置（未改动），已做 YAML/结构校验，但**未在真实 Docker 中构建运行**；容器外
  已用 uvicorn 单进程完成功能与并发验证。
- 目录筛选一次返回全部命中展品（不分页）；标签为精确匹配、区分大小写，无同义词
  或前缀匹配。
- 筛选里的标签按精确匹配处理：某版本中没有任何在线展品携带的标签会出现在
  facet 中且预估为 0（若该标签同时被勾选，取消它即解除其限制）；不做“未知标签”
  报错，避免与“确实 0 件”混淆。
- facet 只列出该版本在线展品携带的标签（另含本次勾选的标签）；仅存在于已下架
  展品的标签不再展示。
- 目录版本只增不减；物理删除展品会破坏按旧版本的预估，因此只提供软下架。
- 同一 `page_view_id` 若出现多个 open，取最早 open；多个 close 取最早 close，
  异常上报不报错但只保留一组配对。
- 没有鉴权/限流与分页；观众号、事件号均按字符串处理，未做格式白名单。
- 撤回是硬删除事件数据（仅保留撤回标记本身），不做审计留档。
