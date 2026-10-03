# 处理多式联运一单制索赔协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

项目同时内置铁水联运一单制责任与索赔服务（`claims.py`）：以统一运单串联分段承运合同、箱组、封识、交接回执、异常证据与责任上限；短少、破损、延误和共同原因拆成独立索赔项目，证据截止后形成责任草案，由相关方确认、申辩或仲裁；部分和解先行支付无争议金额，逾期补交的证据只能触发复审或追加分录，同一异常跨系统重放不会重复赔付；客户与各承运方按最小披露查看材料，监管人员可核对数量与赔款守恒并还原每个责任判断采用的交接回执版本。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、一单制索赔服务、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、索赔协同和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
PYTHONPATH=src python3 -m transport_coordination.claims_acceptance
```

基础验收在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链；索赔验收完整走通运单登记、交接回执多版本、异常跨系统去重、责任草案、申辩仲裁、部分结算、复审追加和监管守恒核对。成功时各输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

一单制索赔域接口：

- 写入：`POST /waybills`、`/handovers`、`/exceptions`、`/claim-items`、`/evidence`、`/liability-drafts`、`/draft-responses`、`/arbitrations`、`/settlements`、`/reviews`、`/review-closures`；
- 查询（按 `X-Actor-Id` 最小披露）：`GET /waybill-view`、`/claim-item-view`、`/handover-history`；
- 监管核对：`GET /conservation-report`、`/decision-basis`。
