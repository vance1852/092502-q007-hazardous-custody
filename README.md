# 危废场所与责任主体服务

维护危废类别、暂存区、责任人员和运输方档案，为危废流转保存一致的基础引用；并在台账之上提供危废交接账本：为每个容器分配稳定身份，记录装载类别、封签、称重和保管责任，支持经授权的拆分与合并，跟踪每一次交出方发起、接收方确认的移交，以及重量或封签差异的容差结案与质量裁决。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：waste_category、storage_zone、custodian_profile、carrier_profile。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 交接账本规则

- **容器身份**：`POST /containers` 为物理容器分配稳定编号，记录危废类别（须已在场所台账登记）、首次装载重量、封签和保管责任方；容器状态为 `open` 或 `consumed`。
- **拆分与合并**：须先由有权人员签发一次性授权（`POST /transformation-authorizations`），再执行 `POST /containers/split` 或 `POST /containers/merge`。拆分要求子容器合计重量与父容器在容差内平衡；合并仅限同场所、同类别、同保管方的容器。所有血缘边写入 `container_lineage`，父容器随即消耗，来源质量全程可追溯。
- **移交联单**：交出方用 `POST /manifests` 发起，申报重量与封签必须与台账一致；接收方用 `POST /manifests/decide` 确认或拒收。重复确认（相同交接号、相同复称结果）只返回原决定；同一交接号下提交不同重量或封签会刷新未决差异并继续进入差异处理。已结案（closed）联单拒绝任何旧消息，不能被重新打开。
- **差异处理**：复称与申报不一致时按封签不符、超差、容差内分级。容差内差异随 `POST /manifests/close` 自动结案并采信接收方复称；超差或封签不符必须由质量人员（reviewer/admin）通过 `POST /discrepancies/rule` 裁决，裁决保留双方陈述。采信则按复称转移保管责任，不采信则退回交出方。
- **溯源查询**：`GET /containers/{id}/trace` 从任一子容器反向还原祖先来源、历次保管事件、未决差异和最终去向（含经后代容器的去向）；`GET /discrepancies?site_id=` 列出未决差异。

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

命令会在临时 SQLite 数据库中登记操作者、场所和领域资料，执行一次"建档 → 授权拆分 → 发起联单 → 复称超差 → 质量裁决 → 结案"的完整交接链，核对幂等回执、已结案联单不可重开、子容器反向溯源与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m hazardous_ledger_core.api --database hazardous_ledger_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持操作者、场所、领域资料、容器、拆分合并授权、联单与差异裁决的登记，以及容器溯源和审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。
