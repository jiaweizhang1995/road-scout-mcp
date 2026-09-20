---
name: road-scout
description: 周边旅行与本地发现。用户询问某地附近的小众景点、民宿、山野空间、本地体验、吃饭去处，或要求寻找与某个参考地点相似的地方时使用。通过 road-scout MCP 只读检索小红书、B站、抖音、公开网页和高德美食榜，并用 Jev 评估第一手体验信号、营销倾向和本次偏好匹配。
version: 1.5.0
---

# road-scout 周边发现

这是个人只读旅行研究能力。默认优先调用已连接的 `road-scout` MCP；只有当前环境没有直接 MCP 工具、但提供 `minis-mcp-cli` 时，才使用 CLI 入口。不要把 MCP 调用包装成任意 shell 命令。

## 默认原则

- 首选高层工具 `road_scout_recommend`，不要让 Agent 手工重复实现搜索、正文读取和 Jev 排序。
- 用户显式偏好优先；只有用户没有说明时，才使用“小众、人少、本地体验、适合自驾”等默认偏好。
- Jev 判断的是第一手体验信号、营销倾向和偏好匹配，不是作者身份认证，也不是事实真实性证明。
- 不只按点赞数排序。低互动内容只要正文有路线、价格、时间、停车、缺点或现场体验，也可以排前。
- 评论用于补充或反驳原帖中的实用信息，不能脱离原帖单独当作事实。
- 某个平台失败时继续使用其他来源；只有失败明显影响答案时才告诉用户，不需要机械汇报所有平台状态。

## 1. `road_scout_recommend`：默认入口

普通的“附近去哪”“找民宿”“周末自驾”“有什么小众地方”等请求，直接调用 `road_scout_recommend`。

传入：

- `request`：尽量保留用户原话，例如“杭州附近100公里小众自驾”“想找安静人少的民宿”。
- `area_name`：城市、景区或地名锚点。已有定位时使用城市/区域名；没有任何地点信息时再向用户询问，不要猜。
- `categories`：按需求设置；普通旅行可使用 `["山野", "民宿", "本地体验"]`。
- `preferences`：传用户真正表达的偏好。不要为了迎合默认模板自动追加“禅意”“寺院”“静修”等词。
- `include_food`：包含吃饭需求时设为 true。
- `max_results`：正式推荐上限，默认 5。候选不够好时返回更少，不凑数。
- `radius_km`：距离上限（公里）。不传时会尝试从 `request` 解析“附近X公里/X米”；显式传参优先于文本解析。
- `latitude` / `longitude`：起点坐标，需成对提供。缺省用 `area_name` 地理编码（配置了 `AMAP_API_KEY` 走高德，否则 Nominatim）。

距离语义：

- 每条推荐带 `distance_km`；超出半径的候选降级到 `exploratory` 并在 `risks` 里标注距离。
- 地点无法地理编码时 `distance_km` 为 `null` 并标“距离未知”，不会因为算不出距离被误删。
- 多条笔记指向同一地点时合并为一条，`mentions` 记录被合并的笔记数。

返回语义：

- `recommendations`：正式推荐，只包含 `supported` 或 `marketing_risk`。
- `exploratory`：`insufficient`（证据不足）或 `out_of_range`（超距离），只能作为备选线索。
- `filtered`：已被筛掉，不应重新放回推荐。
- `food`：高德美食榜候选，仅在美食意图下使用。
- `source_status` / `notes`：用于判断覆盖是否受影响，不必全部展示给用户。

不要在高层结果之后再自己发明第二套打分公式。优先使用工具已经给出的 `reason`、`evidence_status`、`key_evidence` 和 `risks`。

## 2. “找像 X 一样的地方”：先理解参考地点

当用户说“找几个像 X 一样的地方”时，不要直接把“像 X”当成完整偏好，也不要擅自给 X 加上禅意、网红、治愈等风格标签。

先做一次轻量参考地点研究：

1. 用 `social_search` 搜索“参考地点名 + 所在区域 + 实际体验”。
2. 若有可读取的小红书结果，用 `xiaohongshu_note` 看 1–3 条代表性正文。
3. 从用户原话和原始证据中提取 2–5 个具体特征，例如：临溪、老宅、山谷、茶空间、安静、可住宿、适合散步。
4. 再调用 `road_scout_recommend`，把这些具体特征放入 `preferences`，必要时调整 `categories`；`request` 中保留参考地点名称。
5. 如果参考地点证据拿不到，只使用用户明确说出的特征，不自行补全风格。

## 3. 美食请求

纯“吃什么 / 找餐馆 / 本地美食”请求：

- 调用 `road_scout_recommend` 时优先设置 `categories=["本地美食"]`、`include_food=true`，并提供明确的 `area_name`。
- 回答以 `food` 为主，社交来源用于补充具体体验；不要同时输出无关的山野或民宿结果。
- 高德榜单是候选来源，不代表当前一定营业，也不代表排名就是最终质量结论。营业、排队、停车等没有证据时明确写“未核实”。

旅行 + 吃饭的混合请求，可以保留旅行 categories，同时设置 `include_food=true`。

## 4. 低层工具：只在需要时使用

- `social_search(query, sources, limit)`：补充搜索、研究参考地点、用户明确点名某个平台。
- `nearby_discover`：旧的底层 evidence 收集入口；正常推荐优先使用 `road_scout_recommend`。
- `xiaohongshu_note(note_url)`：读取正文。必须使用 search 返回的完整 signed URL，不从 note ID 重建。
- `xiaohongshu_comments(note_url, limit, with_replies)`：核验停车、门票、排队、路线、争议等关键细节时少量使用。
- `douyin_search(query)` / `douyin_creator_comments(sec_uid, ...)`：补充近期现场线索；没有可靠 `sec_uid` 时不要猜。
- `jev_rank_candidates(candidates, user_preferences)`：手工收集候选时使用；正常高层流程已经内置 Jev。
- `gaode_food_ranking(city_name)`：单独补查餐馆榜单。
- `road_scout_status`：首次诊断、调用失败或用户明确要求排查时使用。

## 5. 输出要求

普通推荐以 3–5 个高质量结果为主；没有足够候选时宁可少给。

每条正式推荐优先包含：

- 名称和简短推荐理由
- 来源平台与原始链接
- 1–2 条 `key_evidence`
- 证据状态：有第一手体验支持，或存在营销/合作风险
- 对本次偏好的匹配点
- 有证据时提供停车、门票、营业、路线、信号等实用信息
- 没有证据的实用信息明确标记“未核实”，不要补造

默认不要把未经领域校准的 Jev 精确概率当成结论展示。用户明确要求评分或调试时，再展示 `firsthand`、`marketing`、`fit` 的原始信号。

`exploratory` 必须和正式推荐分开，明确写成“备选线索 / 证据不足”。不要把 `filtered` 候选重新推荐给用户。

## 6. 降级处理

- Jev 不可用：可以展示少量 `unranked` / exploratory 线索，但明确说明未完成质量筛选，不自行假装排好名。
- 小红书正文读取失败：保留其他来源或备选线索，不把标题当正文。
- 小红书评论失败：保留原帖证据，不把缺评论解释成负面。
- 抖音失败：继续使用小红书、B站和网页。
- 高德为空：美食请求可保留其他来源，但不要伪造榜单或营业状态。
- 纯美食请求即使社交来源为空，只要 `food` 有结果，仍应正常回答餐馆候选。
- 如果正式推荐为空，直接说明“没有筛到足够可靠的推荐”；有 `exploratory` 时可以单独给少量备选线索。

## 7. 只读边界

- 不发帖、不评论、不点赞、不收藏、不关注、不修改用户账号。
- 不主动执行登录，不导出 Cookie；可以复用用户已经建立并明确控制的 Chrome 登录会话。
- 不在聊天中回显 MCP token、Jev API key、Cookie 等凭据。
- signed URL 仅作为读取和来源链接使用，不单独提取、解释或传播其中的签名参数。
