# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
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
- `POST /api/items/{id}/evidence`：证据归档，登记自编号、采集人、封存号、载体、存放位置和摘要
- `GET /api/items/{id}/evidence`
- `POST /api/evidence/{id}/loans`：调阅，写明借阅人、用途和归还时限
- `POST /api/loans/{id}/return`：归还，由借阅人以外的另一人核对封条与摘要，异常转待核
- `POST /api/evidence/{id}/reviews`：待核材料复核
- `POST /api/reviews/{id}/correct`：更正原结论，原复核失效留档
- `GET /api/evidence/{id}/reviews`：复核记录（含失效留档）
- `GET /api/custody/pending`：按事故分组的待归还与待核材料

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 证据保管链

材料按事故和自编号归档，同一封存号被未结事故占用时登记退回，事故关闭后封存号可再用。调阅限定在库材料，归还核对异常的材料转待核，复核后回库；原结论更正后原复核失效但留档可查。事故关闭前外借材料必须全部归还，否则关闭被阻止。演示页按事故显示待归还（含逾期标记）和待核材料。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
