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

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 归并与拆回

同一物理交会被本站和外部目录重复建档时，协调员可通过 `merge` 动作（`other_item_id`、`other_expected_version`）把物体对相同、交会时刻相近（默认窗口 300 秒）的两条记录归并为一个协调案：

- 双方来源记录全部留证，协调案通过 `merged_sources` 汇总查询。
- 当前依据按观测时刻较新的那条确定，晚到的旧观测只进 `basis_history`。
- 归并或 `split` 拆回时，同一颗卫星的燃料占用重新结算；超预算的方案退回并在 `details.reservations` 中列出哪条先占。
- 轨道依据一旦更新，依赖它的批准立即失效重算；已下发的规避指令保留原依据。
- 两个协调员同时提交归并时只放行一次，另一边收到 409 及冲突项、最新版次。
- 运营方只能对本机构物体表态，越权表态被拒绝并写入审计链（`opinion_rejected`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道和运营方意见冲突，以及归并去重、依据新旧判定、批准失效重算、燃料重新结算、拆回、并发归并冲突和越权表态审计。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
