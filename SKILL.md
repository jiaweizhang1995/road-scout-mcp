---
name: road-scout
description: 周边自驾旅行发现。用户询问当前位置或某地附近的小众景点、民宿、山野空间、本地吃饭、安静去处，或要求寻找与某个参考地点相似的地方时使用。通过 road-scout MCP 只读检索小红书、高德美食榜和公开网页，并用 Jev 过滤营销内容、判断真实体验和偏好匹配。
version: 1.4.1
---

# road-scout 周边发现

这是只读旅行研究能力。优先直接调用已连接的 `road-scout` MCP 工具；只有当前环境没有直接 MCP 工具、但提供了 `minis-mcp-cli` 时，才使用 CLI 调用。不要把 MCP 调用包装成任意 shell 命令。

## 偏好

- 默认偏好：小众、冷门、人少、有趣、真实体验、适合自驾。
- 不要默认把推荐解释成禅修或静修；只有用户明确提到甘露别院、禅意、寺院或类似风格时才加入这些维度。
- 小红书用于发现景点、民宿、山野空间和小众体验；抖音用于补充近期短视频和地点线索；吃饭推荐优先使用高德美食榜。
- 小红书评论区是重要证据源，重点关注路线、停车、门票、营业时间、排队、交通和负面体验；评论不能脱离原帖单独当作事实。
- 降低只有夸张形容、团购导流、私信预订、旅行社模板、商家自营账号内容的权重。

## 工具选择

### 1. `road_scout_recommend`：首选高层入口

用户问“附近去哪、小众景点、民宿、像某地这样的地方、吃什么”时，直接调用这一个工具。它内部完成：少量查询 → 多源搜索 → 去重 → 读小红书正文 → Jev 筛选 → 必要时补评论/补搜 → 输出正式推荐和备选线索。不要把返回结果再拼一层 raw evidence。

传入：

- `request`：用户原话，例如“杭州附近100公里小众自驾”“想找安静人少的民宿”。
- `area_name`：地点锚点（城市/景区/地名）。有定位时传城市名；用户问“像某地”时传该地所属区域或把地名留在 request 里。没有地点信息时先向用户要，不要猜。
- `categories`：默认 `["山野", "民宿", "本地体验"]`，可按需求覆盖。
- `preferences`：默认小众、人少、本地体验、适合自驾；用户显式偏好优先。
- `include_food`：用户明确问吃饭/餐馆/美食时传 true（request 含“吃/餐/美食/饭”会自动触发）。
- `max_results`：正式推荐上限，默认 5；候选不够好时返回更少，不凑数。

返回 `recommendations`（supported / marketing_risk，含 reason、Jev 信号、key_evidence、risks、原始链接）、`exploratory`（证据不足的备选线索）、`food`（高德榜，仅问吃饭时）、`source_status`、`notes`。filtered 候选不会出现在推荐里。

### 2. 低层工具：调试、补查、用户点名平台时用

- `social_search(query, sources, limit)`：多源搜索原始结果；`nearby_discover` 是按分类批量收集 evidence 的旧入口，用途相同但更底层。
- `xiaohongshu_note(note_url)`：读取单篇小红书正文；`note_url` 必须是 search 返回的完整 signed URL（含 `xsec_token`），不要从 note ID 重建。
- `xiaohongshu_comments(note_url, limit, with_replies)`：需要核验停车/门票/排队/争议时少量补评论，不要对全部召回无差别拉取。
- `douyin_search(query)` / `douyin_creator_comments(sec_uid, ...)`：近期视频补充；没有 `sec_uid` 时明确标记评论未拉取。
- `jev_rank_candidates(candidates, user_preferences)`：对手动收集的候选跑 Jev 三维判断（firsthand / marketing / fit）；正文缺失标记 insufficient，不强行判低质。
- `gaode_food_ranking(city_name)`：吃饭推荐的高德榜单候选；名次不代表实时营业，提醒用户确认营业、排队和停车。
- `road_scout_status`：首次使用、调用失败或用户要求诊断时用，只提炼可操作的故障原因。

## 推荐流程

1. 确定地点锚点（定位城市名或用户提到的地名）和需求描述。
2. 调用 `road_scout_recommend`；问吃饭时确保 `area_name` 有值。
3. 检查 `source_status` 和 `notes`，向用户说明失败的来源。
4. 输出 `recommendations` 为主，`exploratory` 单独标为备选线索；吃饭候选看 `food`。
5. 仅当用户追问细节或某个候选需要核验时，再用低层工具补查。

## 输出要求

每条推荐包含：

- 名称、地点和推荐理由
- 来源平台与原始链接
- `firsthand`、`marketing`、`fit` 判断和简短 reason
- `evidence_status`：有第一手体验支持、营销倾向较高、证据不足或已过滤
- 自驾、停车、营业时间、路线或信号风险
- 明确标注“有第一手体验支持”“营销倾向较高”或“证据不足”

不要只按点赞数排序。小众候选即使互动量不高，只要正文有具体路线、价格、时间、停车、缺点或实际体验，也可以进入前排。

## 红线

只读操作：不发帖、不评论、不点赞、不收藏、不关注、不登录、不导出 Cookie、不修改用户账号。

## 故障排查

- `401 unauthorized`：检查 MCP 连接配置中的 Bearer token，确认没有把 Jev 的 `TYPESAFE_API_KEY` 当成 MCP token。不要在聊天中回显 token。
- `connection failed` 或超时：先调用 `road_scout_status`；如果 Mac 或 Chrome 不在线，说明小红书等登录态来源暂时不可用。
- 高德榜为空：保留小红书和网页候选，并明确说明高德榜单暂时不可用，不要伪造餐厅排名。
- 抖音搜索失败：保留其他来源，不要因为短视频适配器失败而中断整次推荐。
- 小红书评论失败或要求登录：保留原帖证据，标记评论区暂不可用，不要把评论缺失解释成负面结论。
- 抖音没有可用 `sec_uid`：保留视频元数据，标记无法拉取评论；不要调用未验证的私有接口。
