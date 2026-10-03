# 处理多式联运一单制索赔协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

在此基础上，项目同时提供**一单制责任与索赔服务**：以统一运单串联分段承运合同与责任上限、箱组、封识、多方交接回执、异常证据和索赔项目；证据截止后形成责任草案，相关方确认、申辩或仲裁；责任决定只追加，迟到证据只能触发复审新版本与追加分录，结算按业务去重键幂等，跨系统重放不会重复赔付。客户与各承运方看到的材料按最小披露返回，监管人员可核对数量与赔款守恒，并还原每个责任判断采用的交接回执版本。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `claims_models.py` / `claims_service.py` / `acceptance_claims.py` 为一单制责任与索赔域。
- `tests/`：基础规则、事务边界、接口路由、索赔域不变量和端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.acceptance_claims
```

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。索赔域验收额外演示铁水联运换装破损场景：三方交接时间各自成为回执版本、重复报案被拒、短少/破损/延误/共同原因拆分索赔、责任上限封顶、部分和解先行支付、迟到证据触发复审追加、重放不重复赔付，以及三方最小披露视角与监管守恒核对。

## 一单制索赔接口

写接口均以 `request_id` 幂等，通过 `X-Actor-Id` 标识操作者：

- `POST /waybills`、`/segments`、`/containers`、`/seals`：统一运单、分段承运合同（含责任上限）、箱组与封识；
- `POST /boundaries`、`/handover-receipts`：交接节点与多方交接回执（同节点不同时间各自成为追加版本）；
- `POST /incidents`（按 `waybill_id + dedup_key` 去重）、`/evidence`（截止后自动标记 `after_cutoff`）、`/claim-items`（shortage/damage/delay/common_cause）；
- `POST /liability-drafts`（证据截止后形成，份额钉住交接回执版本）、`/party-positions`（confirmed/disputed）、`/decisions`（partial_settlement/awarded/rejected，只追加版本）、`/reviews`（凭截止后新证据发起复审）、`/settlement-entries`（补差/追回，业务去重键唯一）；
- `GET /cases/{waybill_id}`：按角色与参与方返回最小披露材料包；
- `GET /reconcile`：监管核对数量与赔款守恒、重复分录键、各决定钉住的交接版本；
- `GET /decisions/{decision_id}/trace`：还原单个责任判断采用的交接回执版本与证据。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
