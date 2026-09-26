# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程，并对事故材料实行证据保管链管理。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量及保管链规则。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链（含证据、调阅、复核表）。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页（含按事故分组的保管链看板）。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

## 证据保管链

- `POST /api/items/{id}/evidence`：归档，登记自编号、采集人、封存号、载体、存放位置和摘要；同一封存号已被未结事故占用时退回，同一事故下自编号唯一。
- `GET /api/items/{id}/evidence`、`GET /api/items/{id}/loans`：按事故查看材料与调阅记录。
- `POST /api/evidence/{id}/checkout`：调阅，写明借阅人、用途和归还时限（`due_at`或`due_in_hours`）。
- `POST /api/loans/{id}/return`：归还，由借阅人之外的人核对封条（`seal_intact`）与摘要（`digest`）；不一致即异常，材料转待核。
- `POST /api/evidence/{id}/review`：对待核材料登记复核结论，材料回到在库。
- `POST /api/reviews/{id}/correct`：更正结论，原复核失效但留档，新复核生效。
- `GET /api/evidence/{id}/reviews`：查看某材料全部复核（含已失效留档）。
- `GET /api/custody`：按事故汇总待归还（含逾期标记）和待核材料，供演示页看板使用。

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。归档、调阅与归还核对由investigator或safety_manager经办，复核与更正限safety_manager；事故关闭前外借材料须全部归还。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
