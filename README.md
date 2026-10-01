# 矿井应急避险与通风协调

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8335`。领域对象包括矿井人员、气体传感、通风设备、逃生通道、避险硐室、事件和处置任务。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8335
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8335/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于合并现场离线记录（修订账），`source_id + record_id` 相同会幂等返回原记录，可带`?batch_id=...`做整批续传。

## 修订账（离线记录）

每条现场记录以`(source_id, record_id)`为稳定身份，修订按`recorded_at`管理：

- **同一记录只认一次**：完全相同的修订重复补传返回`existing`，不产生新数据。
- **新修订取代旧稿**：`recorded_at`更新的修订自动成为当前版本，旧稿标记为`superseded`；晚到的更旧草稿不再覆盖当前值，只记为`stale`（修复回网乱序时的"旧稿覆盖"）。
- **待确认冲突**：同一时刻不同内容（`same_timestamp_divergent`）或与已确认内容不一致（`differs_from_confirmed`）时，记录进入`conflict`状态并列出`pending_conflicts`。
- **已确认内容不可覆盖**：冲突必须通过`POST /api/entities/<id>/actions {"action":"confirm","data":{"rev_id":"..."}}`显式采纳，新修订在确认前不会改动已确认内容。
- **整批续传**：携带同一`batch_id`重试时，已处理项返回`skipped`不再重复处理，只有`failed`项重新执行；修正后的失败项成功后自动勾销挂账。`GET /api/batches/<batch_id>`查看每项状态与`unfinished`列表。

## 门禁规则

- **通风恢复**：`restore`必须提供有效的复测通过（`tested_at`为ISO-8601且`test_result="pass"`），并且设备`area_code`影响区域内没有`active/missing/located`状态人员（即全部撤离、救出），否则拒绝恢复。
- **避险硐室容量**：`occupy`（可带`count`，默认1）不得使`occupants`超过`capacity`；`release`支持部分释放，全部释放后回到`available`。
- **事件关闭拦截**：关闭前若存在`missing/located`人员、活跃任务、未恢复运行的通风设备，或状态为`conflict`的离线记录，一律拒绝关闭。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务、没有待确认离线冲突，并且所有通风设备恢复运行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
