# PR#1 前置审查（戳一戳 + 表情互动菜单）

- **审查对象**：`0d00no0721/qqbot` PR#1，作者 `yakumo-hx`
- **base**：`d56c9174971554bb0cd04bbb03c59038759c80a9`
- **head**：`c16f60032837d08bc1badf84cf9cd26e38eed24d`
- **采纳方式（已定）**：A —— 原样引入新增文件 + 接线由我们照 PR 语义重做
- **本波范围**：① 取上游代码落 `upstream/`；② 只读安全与正确性审查。**不改任何运行时文件、不启用功能。**
- **状态**：`DONE`（骨架建立于 2026-10-13T12:58+08:00；结论补全于 2026-10-13T14:35+08:00；§6 哈希已复核 6/6 一致）

---

## 0. 结论摘要

**一句话结论：可以采纳（方案 A 原样引入新增文件 + 接线由我们重做）。必须修 3 条、建议修 6 条，其中"必须修"全部落在**接线层**——只要按 §5 的方案落，上游 `scripts/interaction_menu.py` 本体（408 行）可以**原样**收下，一个字都不用改。**

- **总体判定**：上游 `interaction_menu.py`（408 行）是**纯事件驱动**实现：无 `requests/urllib/httpx/aiohttp`、无 `llm_pool/model_*`、无 `while True`、无 `call_later/call_at`、无定时清理任务（唯一 `asyncio.create_task` 在 `interaction_menu.py:261`，由事件触发）。全仓唯一模型出口仍只有 `scripts/llm_pool.py`，本 PR **不新增任何外部 egress / 模型调用**。
- **安全面**：未见可被普通群友利用的提权链。poke 强制 `target_id == self_id`（`interaction_menu.py:276`）；权限判定始终用**实际点击者** `user_id`（`:345-348`、`:382`，`_registry.py:43-49` 用 `uid_str` 比对 `admin_ids`）；菜单派发走**同一个** `handle_command`（`:392`），管理员门禁的语义与手打指令完全一致（上游测试 `test_real_admin_command_checks_clicker_not_menu_owner`，`test_interaction_menu.py:369-386`）。
- **必须修（3 条，全在接线层，见 §4.1）**：
  1. **M1**：`reverse_bot.py` 的 notice 分支必须落在 **`else`** 里（`head/reverse_bot.py:599-603`），不能提成无条件调用。
  2. **M2**：`reverse_bot.py` 的 `finally` 必须改成"**当前连接才清全局**"（`head/reverse_bot.py:621-625`）。我们本地现在是无条件清零（`reverse_bot.py:616-618`）⇒ 多连接下 A 断开会把 B 的在线状态清掉，是**本 PR 顺带修掉的老 bug**，必须一并落。
  3. **M3**：补 `isinstance(data, dict)` 护栏（`head/reverse_bot.py:591-592`），避免 `[]`/`"x"`/`123` 这类合法 JSON 在 `:592 data.get` 抛 `AttributeError`（现仅被 `:601-602` 兜住打日志）。
- **建议修（6 条，见 §4.2）**：S1 拒绝菜单项命令指向 `互动菜单` 自身（防自增殖）；S2 `command` 允许 NUL/CQ 文本（知情项，可选收紧）；S3 `group_manager` 的 `FEATURE_DEFAULTS` 与 `_normalize_entry` **必须成对**落；S4 `poke_menu/reaction_menu` **不要**进 `_FEATURE_KEYS`；S5 `_prune()` 饱和扫描 2.9 ms/次（观测项，建议先不改）；S6 配置错误的用户可见性。
- **不改（by design，见 §4.3）**：无轮询/无定时清理、迟到回执一律丢弃、`emoji_id` 只接受正整数且 1..6 项、状态纯内存（重启需重开菜单）、菜单项命令可带固定参数。
- **未改运行时文件的证明**：§6 结束哈希与基线**逐行完全一致（6/6）**，且 4 个新增运行时文件**均未创建**（`exists=False`）。

---

## 1. 上游代码取回清单（含 SHA256）

完整 16 文件表见 [`upstream/MANIFEST.md`](upstream/MANIFEST.md)（字节/行数/SHA256 三列，本波复核一致）。要点摘录：

| 分类 | 文件 | bytes | lines | SHA256 |
|---|---|---|---|---|
| **新增** | `head/scripts/interaction_menu.py` | 17380 | 408 | `E296B8CB5C5AACCA6A80743A48AA2FD7323BD42FA8A6A1607BAD3B629D5A7DB1` |
| **新增** | `head/scripts/test_interaction_menu.py` | 21668 | 490 | `9F850B3AE63D458A4C7AB37FFA2E9FE02B67FF5605E555762C90DA19E95C02E4` |
| **新增** | `head/scripts/commands/interaction.py` | 613 | 17 | `7A76F72D412E7BC258F2D2693C3642FAFCD3F4EA05D65CFB7359353978AD3F48` |
| **新增** | `head/config/interaction_menu.json` | 541 | 17 | `392B39A8AD5DF91B57E7610D7F7B1009461AEBF1CFCDA5526DDAB85AA07FB34F` |
| 改动 | `head/reverse_bot.py` | 27488 | 671 | `BDA5C6A30723CD0054D11943D37DB53E6D39F718A7A7F9EAF0479096C71D3CA9` |
| 改动 | `head/scripts/commands/_state.py` | 6013 | 176 | `175723726F120FB0377AEF7A05DBAA0472B4EB81FF8BCB896B81F7EF42744F6C` |
| 改动 | `head/scripts/commands/__init__.py` | 943 | 27 | `8536171B207D2F7522B02B487440D7DF4D8016C0A50EA97A35B47E67B7F26926` |
| 改动 | `head/scripts/commands/help.py` | 4586 | 103 | `B8F2D9E030670D26D7F1FCDCEC7978A8C39E9A8021A20F1187841921EC1B990F` |
| 改动 | `head/scripts/group_manager.py` | 15454 | 520 | `DB7A8495FDC9AEE2BB0243BE174F3E6071AF99A614F9E79532729CB9E892FA4D` |
| 改动 | `head/README.md` | 14789 | 295 | `91B6E27DDF260CD4A8601A3D65784B12A78690E982264E27AEE26D94FFE8F354` |

**本地 base 侧交叉校验（证明 base 就是我们的底座）**：`upstream/base/scripts/commands/__init__.py`、`help.py`、`group_manager.py` 三个文件与我们**当前生产文件 SHA256 完全相同**（`32172FB9…` / `2949FD45…` / `87A78EDF…`）⇒ base 取自我们仓库的同一提交，diff 可信。

### 1.1 新增文件 `py_compile` 复验（本波实跑）

```powershell
& .\venv\Scripts\python -m py_compile `
  qq_native_re\findings\pr1\upstream\head\scripts\interaction_menu.py `
  qq_native_re\findings\pr1\upstream\head\scripts\test_interaction_menu.py `
  qq_native_re\findings\pr1\upstream\head\scripts\commands\interaction.py
# exit=0
```

编译产物已归档到 [`_dl/compile/`](_dl/compile/)（`interaction_menu.cpython-314.pyc` 29798B、`test_interaction_menu.cpython-314.pyc` 47375B、`interaction.cpython-314.pyc` 1404B）。上游 head 树内临时产生的 `__pycache__` 已清掉，不留痕。

> ⚠️ 注意：`.pyc` 用的是 **CPython 3.14**（venv 版本），而生产 venv 即此版本，故语法面结论对生产同样成立。

---

## 2. 本地重算 diff 对账

对账口径：`git diff --no-index --numstat base/<f> head/<f>`（只读，不写索引），新增文件按"整文件即新增行"计。

### 2.1 逐文件 ±行数

| 文件 | + | − | diff 产物 |
|---|---|---|---|
| `reverse_bot.py` | 15 | 4 | `diff/reverse_bot.py.diff` (2740B) |
| `README.md` | 65 | 4 | `diff/README.md.diff` (7249B) |
| `scripts/commands/_state.py` | 4 | 1 | `diff/scripts__commands___state.py.diff` (1073B) |
| `scripts/commands/__init__.py` | 1 | 0 | `diff/scripts__commands____init__.py.diff` (570B) |
| `scripts/commands/help.py` | 1 | 0 | `diff/scripts__commands__help.py.diff` (846B) |
| `scripts/group_manager.py` | 20 | 12 | `diff/scripts__group_manager.py.diff` (5170B) |
| **6 个改动文件小计** | **106** | **21** | |
| `scripts/interaction_menu.py`（新增） | 408 | 0 | head 全文 |
| `scripts/test_interaction_menu.py`（新增） | 490 | 0 | head 全文 |
| `scripts/commands/interaction.py`（新增） | 17 | 0 | head 全文 |
| `config/interaction_menu.json`（新增） | 17 | 0 | head 全文 |
| **4 个新增文件小计** | **932** | **0** | |
| **本地重算总计** | **1038** | **21** | |

### 2.2 与 PR 元数据对账

- PR 元数据：`+1038 / −21`。
- 本地重算：`+1038 / −21`（106+932 / 21）。
- **结论：逐行完全吻合，差异 0，无需解释项。** 这同时反证了 `upstream/base` 与 `upstream/head` 两个快照**没有夹带**仓库里其它文件（否则新增行会超出）。

### 2.3 三点必须记住的对账副产品

1. **README 口径的"新增 4 文件"只在 head 里**：`config/interaction_menu.json` 是**新增配置文件**，不是改动既有 `config/group_features.json`。⇒ 接线时不需要动我们现有的 `group_features.json`（新字段按"缺失即关闭"处理，见 §3.f）。
2. **`group_manager.py` 的 20/12 里含一条非本功能改动**：`_normalize_entry()`（`head` 第 104-116 行）从"重建 `{group_name, features}`"改成"`**entry` 展开 + 保留旧字段"（`diff` 第 42-46 行）。这是**我们本地没有的**独立修复（保留 `extra` 等未知字段），采纳时要注意它会顺带改变 `config/group_features.json` 的落盘形状（测试 `test_old_group_fields_preserved_and_new_switches_off` 覆盖，见 `test_interaction_menu.py:471-486`）。
3. **`reverse_bot.py` 的 15/4 不可原样套用**：我们本地 `reverse_bot.py` 与 upstream **base** 已差 `4/1`。**实测差异全文只有两处**（`git diff --no-index base/ reverse_bot.py`）：

```diff
@@ _FEATURE_KEYS @@
-_FEATURE_KEYS = ("decision_engine", "plus_one", "commands", "newcomer_welcome", "profile_record", "vision")
+_FEATURE_KEYS = ("decision_engine", "plus_one", "commands", "newcomer_welcome", "profile_record", "vision", "search")

@@ handle_message: decision_engine.should_reply(...) @@
                 sender_nickname=nickname,
+                # 联网搜索：仅在本群开启 search 功能位**且**本轮是 @bot/别名命中时才有资格搜
+                search_on=_feature_on(gid_str, "search") and bot_mentioned,
```

`_state.py` 与 base 的差异**只有一行**：`VERSION = "2.4.0"` → `"2.5.0"`（我们本地 `_state.py:22`）。⇒ 两条结论：

- **识图（vision）接线在 upstream `base` 里就有**（`base/reverse_bot.py:221-305/402-426`、`base/scripts/commands/_state.py:121-149`），不是我们单方面加的 ⇒ 本 PR 与识图**在线性历史上是先后关系，不冲突**。
- 我们与 base 的实际落差极小（`reverse_bot.py` 4/1、`_state.py` 1/1），因此 PR 的 hunk **几乎可原样套用**，只需在 §5.1 的两处上下文里避让 `search` 改动。

---

## 3. 逐项审查判定表（a–h）

| 项 | 结论 | 证据（文件:行号） | 风险等级 |
|----|------|------------------|---------|
| a) 外部副作用 | **通过（不新增 egress）**：新模块只 import 标准库；唯一外发是既有 websocket；唯一 `create_task` 由事件触发；无 `while True`/`sleep`/`call_later` 定时器。新引入的**动作**只有 2 个 OneBot API（`send_group_msg`、`set_msg_emoji_like`）。 | `interaction_menu.py:8-15`（import 仅 asyncio/json/math/os/time/uuid/OrderedDict/dataclass）；`:162-164` 与 `:306-309`（只有 `websocket.send`）；`:261`（唯一 `create_task`）；`:330-332`（唯一 `set_msg_emoji_like`）；`head/reverse_bot.py:591-594`；`head/scripts/commands/interaction.py:1-17`（不含任何 I/O import）。反证：全文件 grep `requests\|urllib\|httpx\|aiohttp\|http` **零命中**，`llm_pool\|model_` **零命中**。 | 低 |
| b) 事件字段校验 | **通过（边界处理最扎实的一项）**：poke 强制 `target_id == self_id`；reaction 对 8 类畸形输入全部 `return None`；`_number_id` 的 bool/负数/字符串/Unicode 数字边界实测无误。 | poke：`interaction_menu.py:341-342` + `:276-277`（`target_id != self_id → None`）+ `:269`（`uid == self_id → None`，机器人自己戳自己无效）。reaction：`:343`（`notice_type` 精确匹配 + `is_add is not True` 严格比较 ⇒ `"true"`/`1` 不算）、`:345-349`（gid/uid/self_id/mid 及 `uid != self_id`）、`:350-355`（菜单必须是我们登记过的那条 mid，且 `self_id` 一致、未过期）、`:357-358`（功能位）、`:359-362`（`likes` 必须 `list`，元素必须 dict，`emoji_id` 采集成 `set` 后与菜单项比对）、`:364`（配置失效即 `None`）。`_number_id`：`:25-36`。**实测**：`True/False/None/3.0/'abc'/'+'/'+123'` → `""`；`'007'→'7'`、`' 42 '→'42'`、`'１２３'→'123'`、`'١٢٣'→'123'`（Unicode 数字被 `int()` 归一）、`'-100'` 仅 `positive=False` 放行、`'0'` 在 `positive=True` 时拒绝。 | 低 |
| c) 权限归属 | **通过**：权限判定 100% 用**实际点击者** `user_id`；`sender` 由本地构造且**不含 role**，昵称硬编码为"用户"⇒ 无可伪造的角色字段；`nickname` 只用于展示与决策引擎称呼。 | `interaction_menu.py:345-348`（uid 从事件取）、`:381-386`（`"user_id": int(uid)`、`"sender": {"user_id": int(uid), "nickname": "用户"}`，**无 role/card**）、`:380` 注释"不借用菜单发起者/机器人的身份"。下游：`_registry.py:43-49`（`is_admin()` 只比 `self.uid_str in admin_ids`）、`command_handler.py:48-52`（`uid_str = str(data["user_id"])`；nickname 取 `sender.card/nickname`）。全仓 grep `\.role` **零命中**（`sender.role` 未被任何模块消费）。上游测试 `test_interaction_menu.py:369-386` 断言"发起者 20001 是管理员、点击者 20002 时管理员命令不执行且回'只有管理员'"，并断言 `sender={"role":"owner"}` 被忽略。 | 低 |
| d) 越权/注入面 | **通过（无提权链）**，但有 1 条**配置自指**的自增殖玩法需在落库时收紧（见 §4.2 S1）。合成的事件不携带任何特权字段；菜单项命令只经 `handle_command` 这**同一条**链路派发，权限门禁（`is_admin`/`_need_character`）与手打指令完全一致；配置校验足以阻断换行与 CQ 结构注入。 | 派发：`interaction_menu.py:388-394`（`handle_command(websocket, command_data, item.command, item.command)`，`handled=False` 只记日志）；`head/scripts/commands/interaction.py:14-16`（**与戳一戳同一入口** `menus.start_menu`）。管理员门禁仍在下游：`_registry.py:43-49`、`character.py:235/265/297/316`、`character.py:105-121`。配置校验：`:83-84`（items 必须 1..6）、`:89-91`（`emoji_id` 必须正整数且**互不重复**，用 `set` 强制）、`:92-98`（`emoji`/`label`/`command` 均为 1..200 字符**单行**文本，显式拒绝 `\n`、`\r`）。语法面断言：`command` 以 `"message": [{"type":"text",...}]` 形态进入链路，且 `ctx.raw_message` 在本仓库**未被任何 handler 回写**（grep `raw_message` 仅命中 `_registry.py:37` 定义与 `voice.py` 无关路径）⇒ 配置里写 `[CQ:at,qq=all]` 也只是"匹配不到任何指令的纯文本"。 | 低（S1 为低-中） |
| e) 资源上界 | **通过**：菜单/任务/三张缓存/冷却/去重/并发全部有硬上界；无定时清理任务；每个 RPC 都有超时且 `pending` 无泄漏；断线清理完整；迟到回执被主动丢弃。 | 上界常量：`:20-22`（`MAX_ITEMS=6`、`MAX_RATE_RECORDS=4096`、`DEDUP_SECONDS=3.0`）、`:64-67`（5/500/8/10.0，可被配置覆盖）。菜单容量：`:319-324`（先写后裁：每组保留最近 5 条、全局 500 条 FIFO）。并发：`:285`、`:373`（`len(self._tasks) >= max_concurrent_actions` ⇒ **不排队**，直接丢）。缓存：`:240-245`（超 4096 从最旧端弹出）。冷却：`:288`（菜单 30s/组）、`:376-377`（动作 `max(3s, 配置值)`/人）、`:371-374`（`_users_busy` 同人互斥 + `_seen` 重放去重，键含 `time`）、`:378-379`（`_seen` 有效期=菜单 TTL）。RPC 超时与清理：`:155-175`（`wait_for` 含发送耗时；`finally` 必清 `pending` 并 cancel future）。按需清理：`:231-238`（`_prune` 只由事件触发，**无** `call_later`/定时器）。断线：`:398-408`（cancel 本连接在飞任务、清 4 张缓存、清 `_opening`/`_users_busy`）。迟到回执：`:177-186`（`resolve()` 对已过期的 echo 仍返回 `True` ⇒ 丢弃而非路由成消息，避免污染消息链）。**实测开销**：`resolve()` 普通消息 0.113 µs/次、echo 前缀消息 0.254 µs/次；`MenuConfig.get()` 命中态 6.54 µs/次（`os.stat` 主导）；**`_prune()` 在 3×4096 饱和缓存上 2938 µs/次**——且 `handle_notice` 是**在接收循环里同步调用**的，故这是唯一"能被外部事件放大"的开销（唯一观测项，见 §4.2 S5）。 | 低（S5 为观测项） |
| f) 与本项目耦合 | **可接，但 2 处必须改编**（`else` 分支结构 + `finally` 连接归属判断）；`InteractionMenus` 的模块级实例化位置**安全**（依赖已就绪）；`data` 非 dict 的护栏我们本地缺。 | ① 每消息一次 `rpc.resolve`：`head/reverse_bot.py:591-594`，实测 0.113 µs（未命中）/0.254 µs（echo 前缀），无 I/O、无锁，可忽略。② notice 分支：`head/reverse_bot.py:599-603`，`handle_notice` 对**非** poke / 非 `group_msg_emoji_like` 一律 `return None`（`interaction_menu.py:340-344`）⇒ 现有新人欢迎与其它 notice **零影响**；且 `group_increase` 被上层 `if` 吃掉（`:600-601`），poke 不会被误路由。③ 导入顺序：`InteractionMenus` 的构造只用 `self.get_features = get_features`（`:206-213`），**不调用**，故模块级实例化只要求"实例化语句位于 `_get_group_features` 定义之后"。我们本地 `handle_command` 在 `reverse_bot.py:51-56` 导入、`_get_group_features` 在 `:69-87` 定义、模块级状态区在 `:162-171` ⇒ 放在 `:170` 旁**无需改导入顺序**；上游 head 也正是把它放在 `:169-170` 的 `current_websocket` 之后。④ 非 dict / 非 str：`rpc.resolve` 自带 `isinstance(data, dict)` 护栏（`:177-179`），但我们本地 `reverse_bot.py:591-595` **没有** dict 护栏，非 dict JSON 会在 `:592 data.get` 抛 `AttributeError`（被 `:601-602` 兜住，只留一行日志）；`data["raw_message"].strip()`（`:312`）在字段缺失/非 str 时同样只是异常+日志。⑤ `_state.py`：`init_handlers(..., menus=None)` 向后兼容（`head/_state.py:167`、`:170`、`:176`），唯一调用点 `reverse_bot.py:623`（已全仓 grep 确认没有第二处），不传参的后果是"菜单静默不可用"。⑥ `_get_group_features` 返回 `{}` 也安全（`interaction_menu.py:223-225` 用 `.get`；我们本地 `reverse_bot.py:84-87` 保证 dict）。 | **中**（M1/M2） |
| g) 门面契约 | **通过，零破坏**：扩参用关键字默认值，`command_handler.py` 的全部 re-export 未动；`_state.interaction_menus` 是**新增全局**，不在 AGENTS.md 红线名单里，也无需经门面再导出（消费方走 `_state` 属性访问）。 | 新增参数：`head/scripts/commands/_state.py:167`（`menus=None`）、`:170`（`global interaction_menus`）、`:176`（赋值）——默认值保证既有 5 参调用仍合法，属**向后兼容扩参**。调用点：`head/reverse_bot.py:630-631`（**关键字**传参，PR 已同步更新，是唯一调用点）。门面：`command_handler.py:31-40` re-export 名单（`init_handlers/BOT_QQ/VERSION/_dedup_lock/_mystery_queue/processed_msg_ids/PROCESSED_MSG_MAX/known_groups`）**PR 未触碰**；`handle_command`/`process_voice_message`（`:46`、`:28`）入参签名未变。消费方：`head/scripts/commands/interaction.py:10-14`（`_state.interaction_menus` 属性访问，符合 AGENTS.md "子模块经 `_state` 属性访问"约定）。上游测试 `test_interaction_menu.py:388-398` 用 `patch.object(_state, "interaction_menus", ...)` + 真 `handle_message` 走通，反证接线与门面兼容。 | 低 |
| h) 全局状态泄漏（多连接隔离） | **通过（按连接隔离）**：4 张状态表 + echo 表的主键第一位都是 `websocket`；`disconnect` 只清本连接；迟到回执不会跨连接串号。唯一全局额度是 `_tasks`（`max_concurrent_actions=8`）为**跨连接共享**上限，属设计取舍。 | 键设计：`interaction_menu.py:159`（`key=(websocket, echo)`）、`:283`（菜单冷却/占位 `(websocket, gid)`）、`:318`（菜单 `(websocket, gid, mid)`）、`:367`（动作冷却 `(websocket, gid, uid)`）、`:370`（去重 `(websocket, gid, mid, uid, emoji_id, time)`）。回执作用域：`:183`（`self.pending.get((websocket, echo))`）、`:190-193`。清理作用域：`:398-408`（逐表按 `key[0] is websocket` 过滤，`_opening`/`_users_busy` 重建时同样过滤）。全局共享项：`:215`+`:262`（`_tasks` 跨连接计数）、`:285`/`:373`（因此并发额度是全局的）。上游测试 `test_interaction_menu.py:455-468`（`test_echo_is_scoped_to_connection`：别的连接回执不能让本方 future 完成）、`:280-294`（断线清在飞 + 迟到回执被忽略）、`:232-239`（全局容量只淘汰最旧）、`:217-230`（TTL 过期后点击无效）。 | 低 |

---

## 4. 改进清单

### 4.1 必须修（Must-fix）

**M1 — notice 分支必须挂在 `else` 上，不能提成无条件调用**

- **理由**：`handle_notice` 内部第一件事是判 `notice_type`（`interaction_menu.py:340-344`），若提成无条件调用，`group_increase` 会先被 `_safe_handle_group_increase` 消费（`head/reverse_bot.py:599-601`），再被 `handle_notice` 判一次"不是 poke / 不是 emoji_like ⇒ `None`"。功能上暂不炸，但把"新人欢迎"与"互动菜单"耦合进同一条分支，日后任一侧加 notice 类型都会互相干扰。上游用的是 `if group_increase / else handle_notice`。
- **改法**：照 `head/reverse_bot.py:599-603` 原样落（完整片段见 §5.1 A3）。

**M2 — `finally` 必须改成"当前连接才清全局"（同时修掉我们本地的老 bug）**

- **理由**：我们本地是无条件清零：

  ```python
  # reverse_bot.py:616-618（现状）
  finally:
      current_websocket = None
      set_napcat_connected(False)
  ```

  多连接（两台 NapCat / 一条僵尸连接 + 一条活连接）下，**A 断开会把 B 的在线状态清掉**，`user_profile` 侧的 `has_pending_retry/retry_pending_updates` 与"僵尸连接检测"的提示都会失真。上游借此 PR 顺手改成按连接判断（`head/reverse_bot.py:621-625`）。
- **改法**：照 §5.1 A3 落；注意 `interaction_menus.disconnect(websocket)` 必须**放在判断之外**（每个连接断开都要清自己的状态）。

**M3 — 补 `isinstance(data, dict)` 护栏（随 M1/M2 一起落）**

- **理由**：我们本地没有这层护栏，而 M1 之后 `rpc.resolve` 与 `handle_notice` 都会碰 `data`。`resolve` 自带护栏（`interaction_menu.py:177-179`），但 `reverse_bot.py:592` 的 `data.get("post_type")` 在收到 `[]` / `"x"` / `123` 这类合法 JSON 时会抛 `AttributeError`——只会被 `:601-602` 兜住打一行日志，属噪声而非漏洞；加上护栏让"非对象 JSON 直接跳过"，与上游一致（`head/reverse_bot.py:591-592`）。
- **改法**：见 §5.1 A1（3 行）。

### 4.2 建议修（Should-fix）

**S1 — 配置校验拒绝"菜单项命令指向 `互动菜单` 自身"**

- **理由**：菜单项 `command` 可以写 `互动菜单`。此时群友点表情 → `handle_command` → `interaction.py:14-16` → `start_menu` → **再发一条菜单**（含 6 个新表情回应）。每轮占 1 个 `_tasks` 名额、每轮 7 次 API、菜单表按 5/组裁剪。单次收益被 `menu_cooldown_seconds=30`（`:288`，键 `(ws,gid)`）压住，**不构成失控**，但一条配置笔误就能变成"点一下刷一条菜单"的玩法。
- **改法**（改上游新文件，1 行）：在 `MenuConfig._settings` 的循环里加一条拒绝规则。

  ```python
  # interaction_menu.py:_settings() 内，紧接 values 校验之后（现 :98 `values.append(value.strip())` 之后）
              if values[2] == "互动菜单":   # values = [emoji, label, command]
                  raise ValueError("command 不能指向互动菜单自身（会造成菜单自增殖）")
  ```

**S2 — `command` 允许 NUL 等控制字符（**不改**也可，但要知情）**

- **理由**：`:95-97` 只拒绝 `\n`/`\r`。实测 `command = "\x00sign"`、`"[CQ:at,qq=all] x"` 都被**接受**。但它们作为**纯文本**进入 `handle_command`（`:381-386` 走 `"message": [{"type":"text"}]` 结构），而 `ctx.raw_message` 在本仓库没有任何 handler 回写发送 ⇒ **无注入收益**，只会得到"配置的命令未匹配"日志（`:393-394`）。属"配置容错"而非安全问题。
- **改法（可选，1 行）**：

  ```python
  # 同 S1 位置
              if any(ch < " " for ch in values[2]) or "[CQ:" in values[2]:
                  raise ValueError("command 不能包含控制字符或 CQ 码")
  ```

**S3 — `group_manager.py` 的 `FEATURE_DEFAULTS` 与 `_normalize_entry` 要**成对**采纳**

- **理由**：上游把默认值从"全 True"改成"新开关默认 False"（`FEATURE_DEFAULTS`，diff 第 15 行），并让 `_normalize_entry` **不**再把新开关悄悄打开（`head/group_manager.py:107-111`）。如果只采纳前者不采纳后者，管理 CLI **编辑一次**旧群就会把 `poke_menu/reaction_menu` 写成 `True`（因为 `get_default_features()` 兜底来自"新群模板"）。两处必须同进同退；上游测试 `test_interaction_menu.py:471-486` 正是钉这个。
- **改法**：整段照 `head/scripts/group_manager.py` 落（§5.5 给了逐处）。

**S4 — `poke_menu`/`reaction_menu` 不要进 `_FEATURE_KEYS`**

- **理由**：本地 `reverse_bot.py:66` 的 `_FEATURE_KEYS` 决定"这个群是否值得处理消息"（`:334` `if not any(_feature_on(gid_str, k) ...): return`）。若把两个新开关加进去，一个"只开了戳一戳菜单、没开指令响应"的群会通过这道早退，然后**每条消息**都空跑一遍解析（`:337-361`）——而 `interaction_menu.enabled()` 本来就要求 `commands`（`:223-225`），加了也没用。
- **改法**：**保持 `_FEATURE_KEYS` 不变**（只保留现有的 7 项：`decision_engine, plus_one, commands, newcomer_welcome, profile_record, vision, search`）。

**S5 — `_prune()` 的饱和扫描（观测项，可暂不改）**

- **理由**：`_prune`（`:231-238`）每次事件全量重建三张缓存，**实测饱和（3×4096 条）时 2938 µs/次**。两条调用路径的代价不同：
  - `handle_notice`（`:366`）是**在 WS 接收循环里被同步调用**的（`head/reverse_bot.py:602-603` 未 `create_task`）⇒ 饱和时会给接收循环加约 3 ms 的停顿（poke 与表情两类事件都会命中）。3 ms 不会造成危害，但这是本 PR 唯一"能被外部事件放大"的开销。
  - `start_menu`（`:282`）走 `interaction.py:14-16`，在任务里 ⇒ 不阻塞接收循环。
  另外 `_seen` 单表上限是 4096（`:21`/`:244`），饱和需要 4096 个互不相同的 `(gid, mid, uid, emoji_id, time)` 五元组，正常群聊量级下很难达到。
- **改法（可选）**：给 `_prune` 降频（如 `if now - self._last_prune < 1.0: return`）或改成只清"到点的那部分"（小顶堆）。**建议先不改**，上线后看 `[互动菜单]` 日志与 poke 频次再定。

**S6 — 配置错误的可见性**

- **理由**：配置无效时只写一行 warning（`:143-146`），`get()` 返回 `None`，用户侧看到的是"菜单不出现"，管理员难以自诊。
- **改法（可选）**：在 `interaction.py` 的 `cmd_interaction_menu` 里区分"未启用"与"配置无效"，后者回一句"菜单配置无效，请查看机器人日志"。属体验优化。

### 4.3 不改（No-change / by design）

| 项 | 为什么不改 |
|---|---|
| 无历史消息/回应列表轮询、无定时清理任务 | 设计目标即"纯事件驱动"（`interaction_menu.py:2-6` 模块 docstring），`_prune` 按需触发；用户已由上游测试 `test_idle_has_no_new_tasks_network_or_periodic_cleanup`（`test_interaction_menu.py:329-340`）钉死。弱化成定时器反而引入常驻任务与"重启后菜单是否有效"的心智负担。 |
| 迟到/重复回执一律"丢弃而非路由"（`resolve` 恒返回 `True`，`:177-186`） | 这是**必要**设计：`echo` 是我们自己发出去的标记，若返回 `False`，这条带 `echo` 的 JSON 会继续走到 `post_type = data.get("post_type")` ⇒ `None` ⇒ 被静默忽略，看似无害；但一旦 NapCat 在业务回执里带上 `post_type`（实测不会），就会污染消息链。恒 `True` 的语义最稳。 |
| `emoji_id` 只接受正整数、数量 1..6、互不重复（`:83-91`） | 与 NapCat `set_msg_emoji_like` 的 `emoji_id` 语义一致（QQ 表情 ID 为正整数）；上限 6 是因为一条消息下的预设回应展示上限，且每项要发一次 API（`:325-338`）。 |
| 状态全部是进程内内存态、重启即失效（`:5`、`ActiveMenu` 不落盘） | README 已明确"断线清理，重启后需重新打开菜单"（`head/README.md:192`）。落盘会引入"重启后菜单 mid 是否仍有效"的一致性问题——QQ 侧的消息回应是可撤销的，落盘反而更容易产生幽灵状态。 |
| 菜单项 `command` 允许带固定参数、允许多词（`:92-98`） | 上游 README 明确这是特性（`head/README.md:163-164`：`command` 是完整文字，可携带固定参数）。它经同一条 `handle_command` 派发，不扩大权限面。 |
| `_tasks` 并发额度跨连接共享（`:215`/`:285`/`:373`） | 单进程多连接是极少数部署形态（我们生产只有一条 NapCat 连接）。把额度做成按连接会放大总 API 压力，当前"全局 8 个在飞"更保守。 |
| `group_manager.py` 新增 `**entry` 展开（`:112-116`） | 这不是本功能的一部分，而是"保留旧群未知字段"的独立修复（否则编辑群会丢掉 `extra` 等字段）；测试已覆盖（`test_interaction_menu.py:471-486`）。**纳入**，但要在 §5.5 说明它会改变落盘形状。 |

---

## 5. 接线方案（对照我们本地版本）

**总原则**：新增文件**原样拷贝**（4 个，字节级），改动文件**照 PR 语义在我们本地文件上就地改**，不认 hunk 行号（我们与 upstream base 已有 `search` 落差）。全部改动**不改任何函数签名**（除 `_state.init_handlers` 的向后兼容扩参），**不重启进程**、功能位默认关闭 ⇒ 落盘即可编译，启用仍需管理员按群开开关。

### 5.0 新增文件落位（可直接落，4 个）

| 从 | 到 | 校验 |
|---|---|---|
| `qq_native_re/findings/pr1/upstream/head/scripts/interaction_menu.py` | `scripts/interaction_menu.py` | SHA256 必须等于 `E296B8CB…DB1`；408 行 |
| `…/head/scripts/test_interaction_menu.py` | `scripts/test_interaction_menu.py` | `9F850B3A…2E4`；490 行（与现有命名一致：`scripts/test_*.py`，非 pytest 套件） |
| `…/head/scripts/commands/interaction.py` | `scripts/commands/interaction.py` | `7A76F72D…F48`；17 行 |
| `…/head/config/interaction_menu.json` | `config/interaction_menu.json` | `392B39A8…B34F`；17 行 |

> `config/interaction_menu.json` 是**新增文件**，不会与现有 `config/group_features.json` 冲突。

---

### 5.1 `E:\QQbot\reverse_bot.py` —— **需改编（A1-A5 五处落地 + A6 禁止项）**

**A1｜导入区（本地现状 `:25`，紧跟 `import websockets`）**

```python
import websockets
from scripts.interaction_menu import InteractionMenus
```

**A2｜模块级实例化（本地现状 `:169-171`）**

```python
# 运行时状态
current_websocket = None
interaction_menus = InteractionMenus(_get_group_features, handle_command, bot_logger)
```

> **为什么这里安全**：`handle_command` 已在 `:51-56` 导入、`_get_group_features` 已在 `:69-87` 定义；`InteractionMenus.__init__` 只**存引用**不调用（`interaction_menu.py:206-213`），因此不存在"函数未定义"的导入顺序问题。**不要**把它提到 `_get_group_features` 定义之前。

**A3｜主循环（本地现状 `:589-598`）——整块替换**

```python
        async for message in websocket:
            try:
                data = json.loads(message)
                if not isinstance(data, dict):          # M3
                    continue
                if interaction_menus.rpc.resolve(websocket, data):   # 回执拦截，必须早于 post_type 路由
                    continue
                post_type = data.get("post_type")
                if post_type == "message":
                    if data.get("message_type") == "group":
                        asyncio.create_task(_safe_handle_message(websocket, data))
                elif post_type == "notice":
                    if data.get("notice_type") == "group_increase":
                        asyncio.create_task(_safe_handle_group_increase(websocket, data))
                    else:                                # M1：必须挂在 else 上
                        interaction_menus.handle_notice(websocket, data)
            except json.JSONDecodeError:
                bot_logger.info(f"无效 JSON: {message[:100]}")
            except Exception as e:
                bot_logger.info(f"处理消息时出错: {e}")
```

**A4｜`finally`（本地现状 `:616-618`）——整块替换**

```python
    finally:
        interaction_menus.disconnect(websocket)          # M2：每个连接都要清自己的状态
        if current_websocket is websocket:               # M2：只有"当前连接"才允许清全局
            current_websocket = None
            set_napcat_connected(False)
```

**A5｜`main()`（本地现状 `:623`）**

```python
    init_handlers(bot_logger, decision_engine, send_message, send_group_image, send_group_file,
                  menus=interaction_menus)
```

**A6｜不要做的事**

- ❌ 不要把 `poke_menu`/`reaction_menu` 加进 `:66` 的 `_FEATURE_KEYS`（S4）。
- ❌ 不要动 `:334-335` 那道早退、不要动 `:342-361` 的 CQ 解析与图片缓存、不要动 `:402-426` 的识图接线（与本 PR 正交）。
- ❌ 不要给 `_FEATURE_KEYS` 再加 `search`（已在本地 `:66`）。

---

### 5.2 `E:\QQbot\scripts\commands\_state.py` —— **可直接落（2 处）**

**B1｜新增全局（本地现状 `:158-163`，在 `send_group_file_fn = None` 之后）**

```python
# ========== 外部依赖（由 init_handlers 注入） ==========
bot_logger = None
decision_engine = None
send_message_fn = None
send_group_image_fn = None
send_group_file_fn = None
interaction_menus = None                     # ← 新增（PR#1）
```

**B2｜`init_handlers` 扩参（本地现状 `:166-173`）——整块替换**

```python
def init_handlers(logger, de, send_msg_fn, send_img_fn, send_file_fn, menus=None):
    """初始化外部引用（reverse_bot.py 在 main() 中调用一次）"""
    global bot_logger, decision_engine, send_message_fn, send_group_image_fn, send_group_file_fn
    global interaction_menus
    bot_logger = logger
    decision_engine = de
    send_message_fn = send_msg_fn
    send_group_image_fn = send_img_fn
    send_group_file_fn = send_file_fn
    interaction_menus = menus
```

> **兼容性**：`menus=None` 是**关键字默认值**，`scripts/command_handler.py:31-40` 的 re-export 不受影响（只是把一个函数对象再导出，签名变化对导入方透明）；全仓唯一调用点 `reverse_bot.py:623` 由 A5 同步改成关键字传参。**不要**改 `_state.VERSION`（本地 `2.5.0`，上游 base 是 `2.4.0`，不要被 hunk 带回去）。

---

### 5.3 `E:\QQbot\scripts\commands\__init__.py` —— **可直接落（1 行）**

**C1｜文件末尾追加（本地现状 `:26` 之后）**

```python
from . import help  # noqa: F401
from . import interaction  # noqa: F401          # ← 新增（PR#1）
```

> **注意导入顺序即派发优先级**：`interaction` **必须放最后**。它注册的是 `exact` 指令 `互动菜单`，与现有 32 条注册指令无重名——**实测**：静态扫 `@register` 得 32 处（`_registry.py:9` 那处是 docstring 里的示例，不计），导入后 `len(_registry._REGISTRY) == 32`、`dupes == []`（AGENTS.md 里写的 "33" 是文档口径，比实测多 1，与本次接线无关）。放最后可保证即使未来出现同名，既有指令仍优先。

---

### 5.4 `E:\QQbot\scripts\commands\help.py` —— **可直接落（1 行）**

**D1｜主帮助多一行（本地现状 `:80` 之后）**

```python
    for key, short in HELP_SHORT.items():
        lines.append(f"  {key} — {short}")
    lines.append("  互动菜单 — @bot 互动菜单，或戳机器人后点击表情（需管理员按群启用）")   # ← 新增（PR#1）
    reply = "\n".join(lines)
```

> 只加在**主帮助**；`HELP_CATEGORIES`/`HELP_SHORT` 不加新类别（上游也没加，第 81 行是裸 `append`）。上游测试 `test_interaction_menu.py:364-367` 断言主帮助里出现"互动菜单"。

---

### 5.5 `E:\QQbot\scripts\group_manager.py` —— **需改编（6 处，注意与「新群模板」语义耦合）**

我们本地该文件与 upstream **base 完全相同**（SHA256 `87A78EDF…`），因此可以按上游 hunk 落，但**必须整组落**（S3）。

**E1｜`FEATURES` 增两项 + `FEATURE_DEFAULTS`（本地现状 `:27-36`）**

```python
# (key, label, description)
FEATURES = (
    ("decision_engine", "决策引擎", "自动判断是否接话并回复"),
    ("plus_one", "+1 复读", "连续两条相同消息时复读"),
    ("commands", "指令响应", "@bot 帮助、Yau、周礼等所有指令"),
    ("newcomer_welcome", "新人欢迎", "新成员入群时自动欢迎"),
    ("profile_record", "用户画像", "记录该群用户发言用于画像更新"),
    ("poke_menu", "戳一戳菜单", "戳机器人打开菜单（同时需要启用指令响应）"),
    ("reaction_menu", "表情交互", "点击互动菜单的表情执行指令（同时需要启用指令响应）"),
)
FEATURE_KEYS = tuple(f[0] for f in FEATURES)
FEATURE_LABELS = {f[0]: f[1] for f in FEATURES}
FEATURE_DESCS = {f[0]: f[2] for f in FEATURES}
FEATURE_DEFAULTS = {key: key not in ("poke_menu", "reaction_menu") for key in FEATURE_KEYS}
```

> 前 5 项与本地 `:27-33` **逐字一致**（已核对，本 PR 未改它们），只新增后两项与 `FEATURE_DEFAULTS` 一行。

**E2｜`get_default_features()`（本地现状 `:87-95`，`feat.get` 在 `:93`）**

```python
    if isinstance(feat, dict):
        merged = {}
        for key in FEATURE_KEYS:
            merged[key] = feat.get(key, FEATURE_DEFAULTS[key])
        return merged
    return dict(FEATURE_DEFAULTS)
```

**E3｜`_normalize_entry()`（本地现状 `:98-110`）**

```python
def _normalize_entry(entry):
    """确保旧版本数据包含所有功能键。"""
    if not isinstance(entry, dict):
        return {}
    feat = entry.get("features", {})
    defaults = get_default_features()
    merged = dict(feat) if isinstance(feat, dict) else {}
    for key in FEATURE_KEYS:
        # 新群模板不应在编辑旧群时悄悄打开互动功能。
        fallback = False if key in ("poke_menu", "reaction_menu") else defaults[key]
        merged[key] = feat.get(key, fallback) if isinstance(feat, dict) else fallback
    return {
        **entry,
        "group_name": entry.get("group_name", ""),
        "features": merged,
    }
```

> 这里同时引入 `**entry` 展开（保留 `extra` 等未知字段）。落盘形状变化见 §4.3 最后一行。

**E4｜UI 五处兜底值改 `FEATURE_DEFAULTS[key]`**：`print_group_list()`（本地 `:196`）、`print_group_detail()`（本地 `:215`）、`cmd_edit()`（本地 `:274`、`:292`）、`cmd_set_defaults()`（本地 `:348`、`:366`）——把 `feat.get(key, True)` 改为 `feat.get(key, FEATURE_DEFAULTS[key])`，**一处不漏**（漏一处就会在 UI 上把"默认关闭"显示成"启用"）。

**E5｜表头（本地 `:182`）**

```python
    header += "  决策  +1   指令  新人  画像  戳戳  表情"
```

**E6｜两处文案/健壮性**

- `cmd_add_group()`：`print(f"已添加群 {gid}，功能按新群默认配置设置。")`（本地 `:338`，原为"默认功能全部启用"）。
- `cmd_set_defaults()`：`feat = defaults.get("features", {})` 之后补 `if not isinstance(feat, dict): feat = {}`（本地 `:345-346` 附近）。

---

### 5.6 落盘后自检（不改运行进程）

```powershell
# 1) 语法与导入自检（AGENTS.md 指定的主检查）
cd E:\QQbot && $env:PYTHONIOENCODING="utf-8"
& ".\venv\Scripts\python" -c "import reverse_bot; print('OK')"
& ".\venv\Scripts\python" -c "import scripts.commands.interaction; from scripts.commands import _state; print(_state.interaction_menus)"

# 2) 上游离线回归（无真实 QQ / 无模型请求）
& ".\venv\Scripts\python" -m unittest scripts.test_interaction_menu -v

# 3) 门面再导出逐个点名（AGENTS.md 红线）
& ".\venv\Scripts\python" -c "from scripts.command_handler import handle_command, process_voice_message, _mystery_queue, _dedup_lock, BOT_QQ, VERSION, init_handlers, processed_msg_ids, PROCESSED_MSG_MAX, known_groups, find_valid_number; print('facade OK')"

# 4) 群功能 CLI 仍可用（不写配置，只看列表）
& ".\venv\Scripts\python" scripts\group_manager.py
```

> **注意**：`import reverse_bot` 会真正启动一次模块级副作用（建日志目录、读 `.env`），但**不会**监听端口（`asyncio.run` 在 `__main__` 里）。自检期间**不要**触碰正在跑的 `:8080` 进程。

### 5.7 启用与回滚

- **启用**：`python scripts\group_manager.py` → 编辑目标群 → 打开 `指令响应` + `戳一戳菜单` + `表情交互`（三个都要开；`commands` 是硬前置）。默认关闭，未配置的群**完全不受影响**（白名单语义 + `FEATURE_DEFAULTS` 为 `False`）。
- **回滚**：把三个开关关掉即"功能下线"；若要彻底回滚代码，只需恢复 `reverse_bot.py`（删 A1-A5）并移除 4 个新文件；`_state.py` 的 `menus` 参数可留着（默认 `None`，无副作用）。

---

## 6. 未改运行时文件声明

基线哈希（本波开始前，2026-10-13T12:58+08:00，SHA256）：

```
A1D9E0B60532362287919D29D3509DD7115728596379E5E349AD3B34570DF332  E:\QQbot\reverse_bot.py
C1DE3B961FC4F9BDBB77DD30E81B0AB36EB4C9304C969745A82CBFC83B6153A8  E:\QQbot\scripts\commands\_state.py
32172FB926F4AAB5F8E756089794F1FC4733CCDC390D80B93D802A124B772197  E:\QQbot\scripts\commands\__init__.py
2949FD4553E4A5DE32FD9810143B53572DAF69EF5CBAB3EFB5BAFCD586B04B2D  E:\QQbot\scripts\commands\help.py
87A78EDF55943D7BCE6B82EAE386FC0B7E19B169CA7C3BC09412ED16A9B9B70C  E:\QQbot\scripts\group_manager.py
EE82E9B7AAE99F1A54A1BEB03AB9B12551E28EA6A758C4951D63A4FDE48DAD1E  E:\QQbot\scripts\command_handler.py
```

结束哈希（本波结束时实测，2026-10-13T14:2x+08:00，SHA256）：

```
A1D9E0B60532362287919D29D3509DD7115728596379E5E349AD3B34570DF332  E:\QQbot\reverse_bot.py
C1DE3B961FC4F9BDBB77DD30E81B0AB36EB4C9304C969745A82CBFC83B6153A8  E:\QQbot\scripts\commands\_state.py
32172FB926F4AAB5F8E756089794F1FC4733CCDC390D80B93D802A124B772197  E:\QQbot\scripts\commands\__init__.py
2949FD4553E4A5DE32FD9810143B53572DAF69EF5CBAB3EFB5BAFCD586B04B2D  E:\QQbot\scripts\commands\help.py
87A78EDF55943D7BCE6B82EAE386FC0B7E19B169CA7C3BC09412ED16A9B9B70C  E:\QQbot\scripts\group_manager.py
EE82E9B7AAE99F1A54A1BEB03AB9B12551E28EA6A758C4951D63A4FDE48DAD1E  E:\QQbot\scripts\command_handler.py
```

**比对结果：6/6 逐行完全一致（`ALL_SIX_MATCH_BASELINE = True`）。** 本波（含上一任的取证阶段）**未修改任何运行时文件**，也**未创建**任何属于本 PR 的运行时文件：

```
scripts\interaction_menu.py        exists=False
scripts\test_interaction_menu.py   exists=False
scripts\commands\interaction.py    exists=False
config\interaction_menu.json       exists=False
```

本波写入仅限 `qq_native_re/findings/pr1/**`：

| 写入路径 | 说明 |
|---|---|
| `REVIEW.md` | 本文件（§0–§6 补全；原 80 行骨架就地续写，未重建） |
| `_dl/compile/interaction_menu.cpython-314.pyc` 等 3 个 | `py_compile` 产物（§1.1） |
| `_dl/` 下临时探针文件 | 已用后即删（`cfg_probe.json`），无残留 |

`upstream/head/**` 下 `py_compile` 曾生成的 `__pycache__` 已清除（复查 `leftover pycache in upstream:` 为空）。全程**未重启任何进程**（生产 bot 与 NapCat 未受影响），**未执行任何 git 写操作**（`.git` 只读，仅用 `git diff --no-index` 做只读比对）。
