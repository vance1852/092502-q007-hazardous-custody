# 危废场所与责任主体服务

维护危废类别、暂存区、责任人员和运输方档案，为危废流转保存一致的基础引用；并在台账之上提供危废交接账本：容器从车间转入暂存区再交给运输方的每一次移交都有稳定身份、称重与保管责任记录，重量或封签差异可定位到具体交接。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：waste_category、storage_zone、custodian_profile、carrier_profile。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 交接账本

在基础台账之上，`LedgerService` 实现以下规则：

- **容器稳定身份**：每个容器登记装载类别、封签号、皮重/净重（克）和保管责任人；称重（装载、复称、交接申报、交接接收、拆分、合并、裁决）与保管责任变更（装载、交出、接收、退回、拆分、合并）逐条留痕。
- **经授权的拆分与合并**：拆分/合并须先由 reviewer（质量人员）签发一次性授权，operator 执行；拆分要求子容器重量之和与父容器在容差内，合并要求父容器同类别、同暂存区且合并重量与总和在容差内。血缘边记录父子容器与转移重量，来源质量可追溯；授权执行时复核父容器当前状态与重量，已变化则授权失效。
- **交接状态机**：交出方 operator 发起联单（pending），接收方 operator 确认或拒收。确认时逐容器复称：重量与封签一致直接结案；重量差在容差内（`max(tolerance_g, declared × tolerance_ratio)`，默认 5kg 与 1%，可按联单覆盖）自动以接收重量结案并留差异记录；超差或封签不符进入 discrepancy，容器保持在途。拒收使容器退回交出方。
- **重复确认只返回原决定**：联单按决定指纹（decision + 各容器复称重量与封签）识别重放，相同内容返回原决定；同一交接号携带不同重量或封签时，未决联单报冲突，已关闭/已拒收联单登记晚期差异（late_open）但绝不重开。
- **差异裁决**：超差或封签差异须 reviewer 裁决（accept_received / accept_declared / return_to_giver），裁决后联单结案；双方可通过陈述接口留存说明；晚期差异可裁决调整或驳回（dismiss）。
- **反向追溯**：`trace_container` 从任一子容器还原来源链（拆分/合并祖先）、后代、历次保管责任人、全部称重、未决差异和最终去向（在库保管 / 在途联单 / 已拆分合并去向）。

## 目录

- `src/hazardous_ledger_core/`：领域模型、SQLite 存储、权限服务、交接账本、审计链、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

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
PYTHONPATH=src python3 -m hazardous_ledger_core.acceptance
```

命令会在临时 SQLite 数据库中登记操作者、场所和领域资料，登记容器并执行授权拆分，完成一次容差内交接和一次超差交接（陈述、裁决、晚期差异拦截），最后从子容器反向追溯来源与去向，核对幂等回执与审计链。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m hazardous_ledger_core.api --database hazardous_ledger_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者。基础接口：操作者、场所、领域资料登记与审计事件查询。账本接口：

- `POST /containers`、`POST /containers/reweigh`、`GET /containers?site_id=`、`GET /containers/{id}`、`GET /containers/{id}/trace`；
- `POST /remix-authorizations`（reviewer 签发）、`POST /remixes`（operator 执行）；
- `POST /handovers`（交出方发起）、`POST /handover-responses`（接收方确认/拒收）、`GET /handovers/{id}`、`GET /handovers/{id}/statements`；
- `POST /statements`（双方陈述）、`POST /arbitrations`（质量裁决）、`GET /discrepancies?site_id=&status=&handover_id=`。

服务重启后，SQLite 中的业务状态和审计链继续保留。
