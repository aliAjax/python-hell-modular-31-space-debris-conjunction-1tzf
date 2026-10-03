# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`GET /api/cases`、`POST /api/cases`、`GET /api/cases/<id>`、`POST /api/cases/<id>/sources`、`POST /api/cases/<id>/actions` 和 `POST /api/cases/<id>/split`。身份使用 `X-User-Id`、`X-Role`、`X-Org` 请求头。

## 协调案归并

同一物理交会被本站与外部目录各建成一条接近事件时，协调员可将物体对相同、TCA 相差不超过 `MERGE_WINDOW_HOURS`（默认 2 小时）的记录归并为一个协调案（`POST /api/cases`，body 为 `{"item_ids":[...]}`）。

- **来源留证**：各成员记录的来源记录保留在协调案下，可经 `GET /api/cases/<id>` 查看。
- **依据确定**：当前轨道依据取全体来源中观测时刻最新的一条；归并后迟到的更旧观测只进入 `basis_history`，不动当前依据。
- **批准失效**：轨道依据一旦更新，依赖旧依据且未下发的批准立即失效（状态回退、需重算）；已下发的规避指令保留下发时的原依据，不随后续观测改变。
- **燃料结算**：归并/拆回时按同一颗卫星重新结算燃料占用；已下发指令带入协调案，未下发批准随依据更新释放。合计超过预算的方案退回（`fuel_budget_exceeded`），并在 `details.first_occupier` 中列出先占方。
- **并发控制**：归并使用 `BEGIN IMMEDIATE` 事务，两个协调员同时提交只放行一次；落败方返回 `merge_conflict` 与冲突项 `conflict_item_id`、最新版次 `latest_version`。协调案动作通过 `expected_version` 做乐观锁。
- **运营方表态**：操作员只能对本机构（`X-Org`）物体表态，且 `operator` 必须与本机构一致；越权表态返回 `opinion_denied` 并写入审计。

已归并的成员记录不能再直接执行动作（返回 `item_merged`），需通过协调案接口操作。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道和运营方意见冲突。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
