# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

- 作者/评审演示页：<http://127.0.0.1:8101/>
- 主席工作台（独立维护）：<http://127.0.0.1:8101/chair>

默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 角色与分轨

会议按专题分轨。演示数据含两条轨道：`distributed`（分布式系统）、`ai`（人工智能）。

演示用户：

| 用户 | 角色 | 轨道关系 |
| --- | --- | --- |
| `alice`、`bob` | 作者 | 投稿时选择轨道 |
| `r1` | 评审人 | distributed |
| `r2` | 评审人 | distributed + ai |
| `r3` | 评审人 | ai |
| `chair` | 总主席 | 可跨轨查看、邀请、作决定、改轨 |
| `chair_ds` | 分布式轨专题主席 | 只能处理 distributed 轨 |
| `chair_ai` | 人工智能轨专题主席 | 只能处理 ai 轨 |

所有 API 请求带 `X-User-Id` 请求头。

## 主要接口

- `GET /api/tracks`：轨道清单（投稿下拉框使用）。
- `POST /api/papers`：提交论文，必须带 `track_id`。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色与轨道隔离查看；评审人看到双盲视图，旧轨意向/留档分配不授权查看新轨论文。
- `POST /api/papers/{id}/bids`：评审意向，评审人必须是论文所在轨成员。
- `POST /api/papers/{id}/conflicts`：主席（含本轨专题主席）登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行同轨成员、负载上限与冲突检查。
- `POST /api/papers/{id}/move`：**仅总主席**改轨，body 为 `{"target_track_id": "...", "reason": "..."}`。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份**本轨有效**评审后作决定。
- `GET /api/papers/{id}/history`：审计历史，含改轨原因与处置人。

## 改轨规则

- 未接受的邀请（invited/declined）置为 `withdrawn`，从评审人负载中移除，不可再应答。
- 已接受或已完成的评审（accepted/completed）保留评分与意见但置为 `archived=1`：只留档，不再计入新轨的 Rebuttal 与决定。
- 新轨必须重新补足两份本轨有效意见（`archived=0 AND status='completed' AND track_id=论文当前轨`）。
- 已决定的论文不能改轨；同轨改轨返回 409；原因必填（422）。
- **改轨与评审提交同时发生时只让一边成功**：两边都用 `BEGIN IMMEDIATE` + `busy_timeout=0` 抢写锁，败者得到 `409 concurrent_modification`。
- 审计事件 `paper.move_track` 记录原轨道、目标轨道、改轨原因、处置人、撤回与留档的分配 id 列表。

## 业务不变量

评审人不能查看未获本轨有效授权论文的作者身份；利益冲突禁止投标和分配；评审意向与邀请必须落在论文同一轨道；专题主席只能处理本轨论文，总主席可跨轨；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份本轨、非留档的已完成评审；改轨与评审提交互斥，败者返回 409。

## 代码结构（关注点分开维护）

- `domain.py`：异常与时间工具。
- `track_perms.py`：轨道权限（主席管辖范围、评审人轨道成员关系）。
- `assignments.py`：分配事务（邀请、应答、评审提交、改轨与互斥控制）。
- `app.py`：schema/迁移、种子数据、投稿/意向/冲突/反驳/决定与 HTTP 路由。
- `web/index.html`：作者/评审演示页；`web/chair.html`：主席工作台。
