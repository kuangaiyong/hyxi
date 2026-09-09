# 结果页显示主贴原帖链接

## 分诊

**feature** —— 新增外部可见行为（`/posts` 出口多一个字段、结果页多一个可点击元素）。
本项目没有 openspec 根，用户选择把规格产物落在 `docs/` 下，不引入 openspec 目录结构。

## 我读到的需求

1. Facebook 主贴对应的原帖 URL 要在**任务结果页面的帖子列表**里显示 `[描述]`
2. 点击后直接跳到原贴位置 `[描述]`
3. **回复贴不需要**这个链接，只有主贴要 `[描述]`
4. 「跳转后是否需要自动登录，能做到最好；不能自动登录的话，用户登录后自动跳到对应
   url 页面也可以」`[描述]`
5. 链接露出位置：主贴头部右侧一个「🔗 原帖」，与已有的舆情徽标 / `#序号` / 💬回复数
   同排 `[G1 已定]`

## 关于自动登录：不需要，而且不该做

用户给的两条路里，**第二条本来就是 Facebook 自己的行为**，不需要我们做任何事：

- 链接在**用户自己的浏览器**里打开，用的就是他自己的 Facebook 登录态。已登录 → 直达原贴。
- 未登录 → Facebook 自己带着 `next=` 走一遍登录再跳回这条帖子。这正是用户说的
  「用户登录后自动跳到对应 url 页面」。

第一条（我们替他登录）**明确不做**：那意味着把采集用的小号会话 cookie 递进用户浏览器。
采集小号与用户本人不是同一个身份，会话只进不出是这个项目既定的凭据姿态
（见 CLAUDE.md「凭据只进不出」），为了省一次点击破掉它是安全倒退。

## 设计

**URL 现算，不新增存储字段。**`group_id` 在 `sources.params_json` 里，`message_id` 在
`posts` 表里，两样都已经有了。存一份 `url` 列是典型的双写（CLAUDE.md 存储红线第四条），
而且历史数据全都没有那一列 —— 现算则连三个月前采的帖子一起有链接。

站点 URL 形态属于**站点知识**，落在采集器声明里，不进 `results.py`：

| 层 | 改动 |
|---|---|
| `app/collectors/base.py` | 新增 `post_url(source, message_id) -> Optional[str]`，默认 `None` |
| `app/collectors/facebook_group.py` | 覆写：`{base_url}/groups/{gid}/permalink/{mid}/` |
| `app/models.py` | `PostData` 加 `source_url: str = ""` |
| `app/routers/results.py` | `_url_sources()` 取来源记录 + `_post_url()` 现算；`_to_post_data` 填字段 |
| `frontend/src/types/result.ts` | `PostData` 加 `source_url` |
| `frontend/src/views/ResultsView.vue` | 主贴头部右侧「🔗 原帖」链接 |

`/{base}/groups/{gid}/permalink/{mid}/` 是 Facebook 自己的规范形态 —— 用户报的那条
`https://www.facebook.com/groups/2407063016436085/permalink/2494381381037581/` 就是这个
形状，而 `2494381381037581` 正是库里那条主贴的 `message_id`（seq=26，实测核对过）。

`base_url` 取自数据源参数而不是写死常量：本地 fixture 验证时它指向测试站点，
写死会让链接指到真站上去。

## 非目标

- **不给回复贴出链接**。用户明确只要主贴；且每条回复都挂一个链接会把列表塞满。
  Facebook 的评论锚点是 `?comment_id=` 而不是 permalink，形态与主贴不同，
  真要做是另一件事。
- **不给 Tweakers 出链接**。基类返回 `None`，Tweakers 就没有这个按钮 —— 它的帖子 URL
  要 `topic_id + 页码 + 锚点` 三样才拼得出，本次需求也没要。将来要加只需覆写一个方法，
  出口和前端零改动。
- **不改 Excel 导出**。需求说的是「任务结果页面的帖子列表」。
- **不做自动登录**（理由见上）。

## 冻结后的口径

1. `source_url` 只在 `reply_level == 0` 且 `message_id` 非空、且来源还注册着时有值，
   其余一律空串 —— 前端按空串决定不渲染，不需要判断来源类型。
2. 数据源被删掉后历史任务的帖子仍能正常显示，只是没有链接（不报错、不空白）。
3. 链接一律 `target="_blank"` + `rel="noopener noreferrer"`。
4. 位置：主贴头部右侧，`grow` 之后，排在舆情徽标与 `#序号` 之间。

## 任务清单

- [x] `Collector.post_url()` 基类钩子 + `FacebookGroupCollector` 覆写
- [x] `PostData.source_url` + `results.py` 出口填值（只主贴）
- [x] 前端类型 + 主贴头部「🔗 原帖」链接
- [x] 后端测试：主贴有链接 / 回复没有 / 来源删掉后不炸 / Tweakers 没链接
- [x] 真浏览器 E2E：链接渲染出来、href 正确、只挂在主贴上
