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
- 行动项创建时必须登记有效期 `valid_until`（`YYYY-MM-DD`）；完成（`complete`）记录
  `completed_by`，核验（`verify`）记录 `verifier`。

## 投产前安全确认

对已实施（`implemented`）的变更发起 `commission` 时，规则引擎执行投产前安全确认，
任一不满足都会返回 `PreStartupSafetyBlocked`（HTTP 400），响应体 `blockers` 给出
全部阻断项（可多条）：

- `expired`：行动项已核验但 `valid_until` 早于当天（控制措施过期）。
- `validity_missing`：行动项未登记或有效期无法识别。
- `not_verified`：行动项尚未完成独立核验（状态不是 `verified`）。
- `independent_check_missing`：`verifier` 与 `completed_by` 相同，缺少独立校核。
- `safety_review_missing`：高风险（`high`/`critical`）变更未经安全员 `safety_review`。

安全员（`safety` 角色）可对行动项发起 `extend` 延期：

- 必传新的 `valid_until`（必须晚于当前期限）和 `reason`。
- 原期限永久保存在 `original_valid_until`，每次延期追加到 `extensions` 留痕。

投产成功时变更数据写入 `frozen_controls`（控制清单快照，含当时的有效期与校核人）。
投产之后再对冻结行动项 `extend` 不会改动原记录，而是自动生成一条新的 `open`
待办（`follow_up_of` 指向原项），后续改期在新待办上闭环。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
  变更相关动作：`assess` / `approve` / `implement` / `safety_review` / `commission` /
  `rollback` / `close`；行动项相关动作：`complete` / `verify` / `extend` / `reopen`。
- `GET /api/audit`：读取审计记录。
- 演示页面（`/`）支持刷新清单、发起延期/安全员复核/投产并展示成功结果或阻断项明细。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
