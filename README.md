# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。

行动项创建时必须登记有效期 `due_date`（YYYY-MM-DD），`verify` 动作必须记录校核人 `verifier`。

## 投产前安全确认

变更进入 `implemented` 后，`commission`（投产）不再只看行动项是否核验过，还会一次性返回全部阻断项（HTTP 400，响应体含 `blockers` 数组）：

- `control_expired`：控制措施已过当前有效期（需安全员延期）。
- `independent_check_required`：校核人与完成人相同，必须由第三人独立校核。
- `safety_review_required`：高风险（`high`/`critical`）变更缺少安全员复核。
- `action_item_not_verified` / `due_date_not_registered`：行动项未完成校核或未登记有效期。

相关动作：

- 行动项 `extend`：仅 `safety` 角色可执行，需提供 `new_due_date`（必须晚于当前期限）和 `reason`；延期追加进 `extensions` 列表，原 `due_date` 始终保留。
- 变更 `safety_review`：仅 `safety` 角色在 `implemented` 状态执行（状态不变），记录 `safety_reviewed_by`。
- 投产成功时把当前控制清单快照写入变更数据 `frozen_controls`；投产后再对行动项发起 `extend` 不会改动冻结清单，而是自动生成一条新的 `open` 行动项（`rescheduled_from` 指回原项）。

## 演示页面

打开根路径可在页面上生成演示场景（可选过期/自校核/高风险），并直接发起安全员复核、延期、投产；阻断项、冻结结果和新待办都会在页面展示。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
