---
name: hyxi-image-pipeline
description: hyxi 的图片理解与整串上下文（只作用于舆情）：Kimi 真机实测结论、纯图帖处理、导出报告里的配图。动图片理解、舆情配图、Kimi/视觉模型调用之前读这份。
---

## 图片理解与整串上下文（只作用于舆情）

**舆情分析前，带图帖子的配图先交给多模态模型转成中文描述**，再连同正文一起发给大语言模型。
配置走 `app_config` 的 `vision.` 前缀（`get_app_config` 本来就按前缀分组，**没动任何表结构**），
未配置时 `get_vision_service()` 返回 `None`，整条链路降级为纯文本。

- **只有舆情走这条路，翻译不走**。译文对着的是正文，塞图进去只会污染译文，还要为每条带图
  帖子多付一次多模态调用。回归测试见 `TestImageUnderstandingEndToEnd::test_translation_never_touches_the_vision_model`
- **多模态的系统提示词复用翻译那份角色**（`INDUSTRY_ROLE` + `INDUSTRY_GLOSSARY`，从
  `translator_service` import）。看懂储能设备照片、App 截图、配电箱接线图靠的正是那张术语表。
  抽常量时 `TRANSLATION_SYSTEM_PROMPT` 拼出来**必须逐字节不变**，否则几百条帖子的译文口径跟着变
- **prompt 里只让它描述、不让它下结论**（「不要写「用户对此不满」这类结论」）。混在一起会让
  舆情判定被描述者带跑，而那正是下一步要独立做的事
- **描述落 `posts.image_desc` 且重采集不覆盖**（与 `translation` / `sentiment_at` 同规矩）。
  主贴的图会被它下面**每一条回复**的整串上下文引用，不存的话增量每轮都要把同一张图重新买一次
- **失败一律降级不中断**：模型没配、配额 403、图丢了、路径越界 —— 都只是这条没有描述，
  舆情照常按正文跑完。为一张图让整轮分析失败是不可接受的

**回复贴的判定要看整串，不是父贴前 200 字**。`post_tree.thread_of()` 把每条帖子映射到它所属的
完整讨论串（嵌套回复归到顶层主贴那一串），`_post_block()` 渲染成带 `▶` 标记的上下文块，
注明哪一条才是待分析对象。「+1」「same here」单独看必然是 neutral 噪音 —— 实测 60 条回复里
大量是这种。**`thread_by_key` 必须基于 `all_posts` 而不是 `pending_posts`**：增量时待分析的
往往只是一条新回复，只拿它自己组串等于没有上下文。整串块有 `THREAD_CONTEXT_LIMIT`（3000 字符）
上限，裁剪时**主贴与待分析条目永远保留**。

**`/config/vision/test` 成功不代表图片理解可用**：`test_connection()` 先探 `/models`，而
**配额用尽时那个接口照样返回 200**（实测 Kimi 就是这样：`/models` 200，`chat/completions` 403
`access_terminated_error`）。界面文案已经写明这一点，别把它简化掉。

### Kimi 真机实测结论（2026-08-14，配额恢复后对 api.kimi.com 实跑）

**`kimi-for-coding` 支持图片输入**，实测能准确读出 App 弹窗里的荷兰语文案与设备序列号。
`k3` / `k3-256k` 同样可用；`kimi-for-coding-highspeed` 返回 401（订阅层级不含）。
**改这条链路的参数前先读这一段，别照着「常规做法」猜**：

- **不能传 `temperature`**。它只接受 `1`，传 `0.2` 会被整个请求打回
  `400 invalid temperature: only 1 is allowed for this model`，再被降级逻辑吞成
  「这条没有描述」—— 功能看着在跑，一张图都没理解过。各家视觉模型对这个参数约束不同，
  一律交给服务端默认值
- **`max_tokens` 必须给够，因为它是推理模型**。`reasoning_content` 与正文分开返回，
  但**照样计入 `max_tokens`**。实测给 512 时 512 个全被推理吃掉：HTTP **200**、
  `finish_reason=length`、`content` 是**空串**。这比报错难查得多 —— 没有任何异常信号，
  只是所有图片都「理解成功但没有描述」。实测推理约 300~700 token，`MAX_OUTPUT_TOKENS`
  取 2048。`describe_post_images()` 在拿到空描述时会单独打一条警告点名这个原因
- **预算给够了照样会偶发跑飞，所以拿到空描述必须重打一次**。同一张图，模型偶尔陷进长推理
  把整个预算烧在 reasoning 上。**调大预算解决不了**：实测 4096 也被烧穿过（4096 全进推理），
  只是每次跑飞多浪费一倍 token。实测单次失败率约 12%（84 次调用 10 次空），重打一次后
  23 张图的一轮期望漏网不到 1 张；仍漏的那张只是按纯文本分析，不影响整轮
- 这三条都有回归测试，且 `tests/fixtures/vision_site.py` 把三种真机行为一起复刻了
  （temperature 不为 1 就 400；`max_tokens` 低于门限就返回 200 + 空 content；
  `empty_first=N` 让前 N 次无条件跑飞，用来钉住重试）

真机证据（真实帖子 `src_b32bc603` + 真实 31685 字节 JPEG）：多模态给出
「HyXi Halo App设置界面弹窗提示：当前固件版本不支持并联运行，需先升级至最新固件。
弹窗显示设备序列号SN:34302260600373……」，与主贴正文所述完全对得上；该描述随整串上下文
进入文本模型，回复贴拿到 `neutral / 固件更新 + 扩展/兼容性` 的结论。
库里 23 条带图帖子整批复跑（真实库、真实图、真实模型）：不重试 21/23，加上重试 22/23。

**增量粒度仍是 `sentiment_at`，所以改了分析口径必须走 `?force=true`**。接上图片理解那天，
库里 124/125 条已分析、23 条带图的**全部**已分析 —— 不重跑的话新功能在现有数据上一点变化都
看不到。舆情页的「🔄 强制重新分析」就是这个入口，点击弹确认框（它会重新花钱）。

**舆情分析的另一条路是结果页按钮**（`POST /tasks/{id}/sentiment`，后台跑、立刻返回）。两条路共用 `orchestrator.run_sentiment()`，区别只有：流水线那条 `await` 到底并让异常冒出去（步骤要靠它判成败），按钮那条 `create_task` 后立刻返回、失败只发 `sentiment_complete` 事件。`run_sentiment_async()` 必须**同步**把 task_id 放进 `_sentiment_running`——等协程调度起来再放的话，POST 刚返回那一瞬间前端来问 `GET /sentiment` 会得到「没在跑」。

两条路的增量粒度都是 `_processed.sentiment_at` 为空的帖子。跨来源分组（`by_source` / `cross_source`）走**纯 Python**，不需要 LLM 感知来源；prompt 里只给每条帖子标 `[来源: xxx]` 并说明「按平台内部的相对水平判断」，**绝不描述某个平台的情感先验**——那会污染的正是我们想比较的那个维度。评论会带上父贴前 200 字作 `[回复上文: ...]`，否则「+1」「same here」全被判成 neutral 噪音。

**并发控制**：`max_concurrent_tasks` 超限时新任务进 `_task_queue` 排队，前一个任务在 `_run_with_queue` 结束后调 `_process_queue()` 自动出队执行，不会直接失败。

### 纯图帖（一个字都没有、只有一张图）

**「能不能分析」的判据是 `sentiment_service.is_analyzable()`：有正文 or 有配图。**
入库口、流水线、页面按钮三处全部 import 它，不许各写各的 —— 曾经三处都是
`(p.get("content") or "").strip()`，于是纯图帖在四个环节被一致地当成空气。

代价是真实数据丢过：`media/src_b32bc603/6680d5f13a6b2b4c_0.jpg`（HYXi 安装检查报告，
总分 88、发电异常 8/20 标橙）还躺在盘上，`posts` 表里一行都没有 —— 采集脚本先下图、
`drop_empty_posts()` 再丢帖子。它连「未分析」都不显示，是**直接消失**，比留一行空白难发现得多。

**`analyze()` 里筛选与图片理解的先后顺序是这条链路的命门**：

1. 先按 `is_analyzable()` 圈 `candidates`（**必须排在图片理解之前** —— 用正文筛的话纯图帖
   进不了 `need_desc`，它的图永远不会被理解，于是永远分析不了，形成死循环）
2. `_understand_images()` 按 candidates 所在的**讨论串**收集带图帖
3. **理解完之后**才定 `non_empty`：`正文非空 or image_desc 非空`

第 3 步不能省。多模态没配 / 调用失败 / 图丢了的时候，纯图帖是**真的**没有可判断的内容，
送一个空块给 LLM 只会换回一条编出来的结论 —— 那比诚实地留「未分析」糟得多。

正文位置在 prompt 里**不能留空**，用 `NO_TEXT_PLACEHOLDER` 顶上（`_post_block` 与
`_member_line` 共用同一句）：一片空白看起来像一条被截断的帖子，模型会照着「没说什么」判
neutral，而信息其实全在紧随其后的 `[图片: ...]` 里。系统提示词里因此专门写了一条
「不要因为没有正文就一律判 neutral」。

主贴↔回复的关系不用另做 —— `thread_by_key` 已经让纯图主贴的描述随整串上下文进到它每一条
回复的 prompt 里，反过来纯图主贴也拿得到回复。回归测试见 `TestImageOnlyPostsAreAnalyzedEndToEnd`。

### 导出报告里的配图

`EXPORT_COLUMNS` 里「图片描述」「配图」两列由 Excel 和 CSV 共用：CSV 的「配图」是相对路径
文本（照着能在 media 目录里找到原图），Excel 那一格贴 150px 缩略图。大图另开一张
**「配图」工作表**（`IMAGE_SHEET`），一条帖子一段：帖子头 + 完整描述 + 700px 大图 + 返回链接。

**超链接挂不到图片上，别再试**：openpyxl 3.1.5 的 `Image` 只有 `anchor` / `path`，
`SpreadsheetDrawing._picture_frame()` 里的 `cNvPr` 是现场写死的，没有注入 `hlinkClick` 的口子；
Excel 本身也没有「点图放大」的原生行为。所以跳转入口放在同一行的**「图片描述」格**上 ——
它既是纯图帖最显眼的文字，又不会被图片盖住点不着。内部跳转必须走
`Hyperlink(location=...)`，给 `target` 会被当成外部关系，Excel 打开时报「需要修复」。

缩略图**重新编码**（PIL `thumbnail()` → JPEG），大图**用原始字节只钳显示尺寸**：前者若直接
拿原图按 150px 显示，xlsx 里会存两份原始字节；后者若重新编码，等于把要看清的细节又糊一遍。

**大图不能把整张高度压进一行**：Excel 单行上限 409.5pt（≈546px），而真实 Facebook 截图是
367×795 这个量级，钳到 700px 仍有 525pt —— 设过去会被 Excel 截回来、图盖到下面几行上
（实测导出里出现过 25 行 531pt）。所以配图表**不设行高**，按默认行高（15pt≈20px）留够
`ceil(高/20)` 行让图自己铺开。回归测试见
`TestExportImagesEndToEnd::test_tall_image_gets_enough_rows_instead_of_one_oversized_one`。

**导出的图片总量没有上限**，整份工作簿在内存里拼好再作为一个 Response 返回。当前
138 条帖子 / 31 张图 ≈ 0.93MB，够用；来源长期跑下去图数是线性增长的，哪天导出变慢或吃内存，
先看这里而不是先怀疑 LLM。

**纯图帖拿不到描述时不写 `sentiment_at`**，所以下一轮增量会连图片理解一起重试。这与文本帖
分析失败后重试是同一个规矩，没有单独的失败计数 —— 代价是一张**永远**描述不出来的图每轮多烧
两次多模态调用（真机空描述率约 12%，重试一次后残留很低）。真出现这种图，先查图本身。

`openpyxl` 插图硬依赖 **Pillow**（缺了直接 `ImportError`），已进 `requirements.txt`；
`excel_service` 仍兜了一层 try/except，既有 venv 没更新时报告降级成无图而不是下载 500。
路径解析复用 `vision_service.media_path()`（含 realpath 包含性校验）—— `images` 来自采集脚本，
而 media 目录之外就是数据库和明文密钥。

