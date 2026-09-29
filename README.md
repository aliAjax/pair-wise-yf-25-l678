# 学术会议同行评审系统（分轨版）

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

- 投稿/评审首页：<http://127.0.0.1:8101>
- 主席控制台（轨道、邀请、改轨、决定、历史）：<http://127.0.0.1:8101/chair>

默认数据库为 `review.db`，端口为 `8101`。旧版无轨道的数据库启动时会自动迁移。测试：

```bash
python -m unittest -v
```

## 代码组织（三块分开维护）

| 关注点 | 位置 |
| --- | --- |
| 轨道权限（身份、管辖范围、评审团） | `tracks.py` |
| 分配事务（邀请/回应/评审/改轨，统一 `BEGIN IMMEDIATE`） | `assignments.py` |
| 主席页面（控制台 UI） | `web/chair.html` |
| 领域共享内核（错误、时间、常量） | `domain.py` |
| HTTP 路由与其余领域逻辑 | `app.py` |

## 角色与轨道

演示用户：`alice`、`bob`（作者），`r1`/`r2`/`r3`（评审人），`chair`（**总主席**，跨轨），
`chair_db`（数据库轨专题主席）、`chair_sys`（系统轨专题主席）。所有请求带 `X-User-Id`。

- 轨道：`db`（数据库与数据管理，评审团 r1/r2/r3）、`sys`（系统与网络，评审团 r2/r3）。
- `users.role='chair'` 且在 `track_chairs` 中有绑定 → 专题主席，只能处理本轨；
  无绑定（`chair`）→ 总主席，可跨轨调整与决定。
- 改轨**只能由总主席执行**，专题主席越轨操作返回 403 `track_scope_violation`。

## 主要接口

- `GET  /api/tracks`：轨道、专题主席与评审团名单。
- `POST /api/papers`：提交论文，必须带 `track_id`（作者投稿选轨）。
- `GET  /api/papers` / `GET /api/papers/{id}`：按角色与轨道隔离；专题主席只见本轨，评审人为双盲视图。
- `POST /api/papers/{id}/bids`：评审意向，评审人必须属于该轨评审团（意向同轨）。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突（受轨道权限约束）。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行轨道权限、负载上限、冲突检查。
- `GET  /api/papers/{id}/assignments`：主席查看该论文全部邀请/评审（含留档记录）。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/track`：总主席改轨，body 为 `{"track_id":"sys","reason":"..."}`。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal（需 ≥1 份本轨有效评审）。
- `POST /api/papers/{id}/decision`：收到至少两份**本轨有效**评审后作决定。
- `GET  /api/papers/{id}/history`：审计历史（含改轨原因与处置人）。

## 改轨语义与并发规则

改轨（`papers.track_seq` 纪元 +1）时，对当前轨道/纪元的旧邀请按状态处置：

| 改轨前状态 | 改轨后 | 是否计入新轨决定 |
| --- | --- | --- |
| `invited`（未接受） | `withdrawn`（撤回，不能再接受） | 否 |
| `accepted`（已接受未评审） | `archived`（留档，不能补提评审） | 否 |
| `completed`（已完成评审） | `archived`（分数与意见保留可查） | 否 |
| `declined` / 已撤回 | 保持原样 | 否 |

因此改轨后总主席必须在新轨重新邀请，并补足**两份本轨有效评审**才能决定；
同一评审人在新轨可以再次被邀请（分配唯一性按 `(paper_id, reviewer_id, track_seq)` 计）。

**改轨与评审提交互斥**：两边都在 `BEGIN IMMEDIATE` 写事务里先抢保留锁，再核对
`papers.track_seq` 与 `assignments.track_seq`：

- 评审先提交成功、改轨随后成功 → 该评审随改轨归档，只留档、不计入新轨；
- 改轨先提交成功、评审随后提交 → 返回 **409 `paper_moved`**，邀请只作留档。

无论哪种交错，新轨有效评审数都不会被旧轨意见"污染"。改轨动作的审计记录
（`paper.track_change`）包含 `from_track`、`to_track`、`reason`、`handled_by`
以及撤回/归档数量。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；
每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；
评审意向与邀请必须与论文同轨（评审人属于该轨评审团）；专题主席只能处理本轨，
总主席可跨轨；改轨撤回未接受邀请、归档已接受/已完成记录；
决定必须至少基于两份**当前轨道、当前纪元**的已完成评审；改轨与评审提交并发时只让一边成功。
