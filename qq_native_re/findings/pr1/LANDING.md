# PR#1 落盘报告（戳一戳 + 表情互动菜单）

- **落地对象**：`0d00no0721/qqbot` PR#1（base `d56c9174…` / head `c16f6003…`）
- **方案**：A —— 4 个新增文件原样拷入（SHA256 校验）+ 5 个受影响文件按 `REVIEW.md §5` 就地改编
- **本波边界**：只落**代码与配置**；**开关默认全关**、**不启用任何群**、**不重启任何进程**、**未碰 `NapCat/**` 与 `.env`**、**未做任何 git 写操作**
- **状态**：`DONE`（骨架建于 2026-10-13T14:48+08:00；**落盘与生产树重验完成于 2026-10-13T17:00+08:00**）
- **落盘执行**：第 1 次尝试被沙箱写权限挡死（只读、写仅限 `qq_native_re/**`）；用户放宽为 `danger-full-access` 后本次成功落盘 9/9

---

## 0. 开工基线（落盘前实测，2026-10-13T14:48+08:00）

备份目录：`qq_native_re/patches/pr1_land_backup_20261013-144823/`

| 文件 | 状态 | bytes | SHA256（落盘前） | 审查 §6 基线 | 一致 |
|---|---|---|---|---|---|
| `reverse_bot.py` | 待改 | 27907 | `A1D9E0B6…DF332` | `A1D9E0B6…DF332` | ✅ |
| `scripts/commands/_state.py` | 待改 | 5917 | `C1DE3B96…153A8` | `C1DE3B96…153A8` | ✅ |
| `scripts/commands/__init__.py` | 待改 | 903 | `32172FB9…72197` | `32172FB9…72197` | ✅ |
| `scripts/commands/help.py` | 待改 | 4466 | `2949FD45…4B2D` | `2949FD45…4B2D` | ✅ |
| `scripts/group_manager.py` | 待改 | 14750 | `87A78EDF…9B70C` | `87A78EDF…9B70C` | ✅ |
| `scripts/command_handler.py` | **红线，不得改** | 2586 | `EE82E9B7…DAD1E` | `EE82E9B7…DAD1E` | ✅ |
| `config/group_features.json` | **不得改** | 596 | `822B1D7E…CD760` | — | — |
| `scripts/interaction_menu.py` | 不存在 | — | — | — | — |
| `scripts/test_interaction_menu.py` | 不存在 | — | — | — | — |
| `scripts/commands/interaction.py` | 不存在 | — | — | — | — |
| `config/interaction_menu.json` | 不存在 | — | — | — | — |

**进程基线**：`:8080` LISTENING 的 PID = **45892**（`python`，启动于 2026-10-13T08:41:58+08:00）。全波结束须仍为同一 PID。

### 0.1 重派开工基线（2026-10-13T16:47:37+08:00，`danger-full-access` 复测）

上一次落盘 worker 被沙箱写权限挡死（只能读、写仅限 `qq_native_re/**`）；用户已把本会话文件策略放宽为 `danger-full-access`，本次重跑落盘。

| 项 | 实测值 | 判定 |
|---|---|---|
| `:8080` LISTENING Owner PID | **45892**（`python`，StartTime 2026/10/13 8:41:58） | 与基线一致 ✅ |
| `config/group_features.json` SHA256 | `822B1D7E021ED830E7618EB01CD903D7C044E3EA192F020000D33FDB123CD760` | 与基线 `822B1D7E…CD760` 一致 ✅ |
| `scripts/command_handler.py` SHA256 | `EE82E9B7AAE99F1A54A1BEB03AB9B12551E28EA6A758C4951D63A4FDE48DAD1E` | 与基线 `EE82E9B7…DAD1E` 一致 ✅ |
| cwd | `E:\QQbot` | ✅ |

**校验脚本路径结论（重要）**：`check_facade.py` 硬解析脚本上三级 = `E:\QQbot`（**始终为生产树**）；`check_wiring.py` / `check_s1.py` 优先取 cwd，当 cwd=`E:\QQbot` 且 `scripts/interaction_menu.py` 存在时 root=生产树，否则同样退回 `E:\QQbot`。⇒ **三个校验脚本无需改写即可对生产树运行**。

---

## 1. 新增 4 文件（原样拷入 + SHA256 校验）

本次落盘时间 2026-10-13T16:55+08:00，回滚备份目录 `qq_native_re/patches/pr1_land_backup_20261013-165506/`。
落盘前已用 `_pr1_stage/diffs/*.diff` 逐个比对：**生产的 `reverse_bot.py` / `group_manager.py` / `_state.py` / `commands/__init__.py` / `help.py` 与 `patched/` 之差恰好等于 staged diff**（唯一额外差异是 `reverse_bot.py` 末行 EOF 换行，属良性）。
⇒ **未回退并行 search 工作流的任何改动**：生产 `reverse_bot.py:66` 的 `_FEATURE_KEYS` 已含 `"search"`，`patched/reverse_bot.py` 同样含 `search`，故 `patched = 生产 + PR#1 hunks`，拷入是纯增量。

| 目标（生产路径） | 来源 | bytes | SHA256（落盘后，= 来源副本） | src==dst |
|---|---|---|---|---|
| `scripts/interaction_menu.py` | `patched/scripts/interaction_menu.py`（含 S1） | 17608 | `5D1E7321967BAF47BCBD993FDD02101CDADE9892FB1C0BD206CC5B5F19A60850` | ✅ |
| `scripts/commands/interaction.py` | `stage/scripts/commands/interaction.py` | 613 | `7A76F72D412E7BC258F2D2693C3642FAFCD3F4EA05D65CFB7359353978AD3F48` | ✅ |
| `scripts/test_interaction_menu.py` | `stage/scripts/test_interaction_menu.py` | 21668 | `9F850B3AE63D458A4C7AB37FFA2E9FE02B67FF5605E555762C90DA19E95C02E4` | ✅ |
| `config/interaction_menu.json` | `stage/config/interaction_menu.json` | 541 | `392B39A8AD5DF91B57E7610D7F7B1009461AEBF1CFCDA5526DDAB85AA07FB34F` | ✅ |

`ALL_SRC_EQ_DST=True`（9/9）。

## 2. `reverse_bot.py` 改编（A1–A5 + M1/M2/M3）

| 目标 | 来源 | bytes | SHA256（落盘后） | src==dst |
|---|---|---|---|---|
| `reverse_bot.py` | `patched/reverse_bot.py` | 28708 | `A3DA829E340A9D690489FF09E7E55D2261974E4492FD3A271C0090A17EC8C64D` | ✅ |

含 A1（`from scripts.interaction_menu import InteractionMenus`）、A2（`interaction_menus = InteractionMenus(_get_group_features, handle_command, bot_logger)`）、A3（`rpc.resolve` 回执拦截，早于 `post_type` 路由）、A4（`init_handlers(..., menus=interaction_menus)`）、A5、M1（`handle_notice` 挂 `else`）、M2（`disconnect(websocket)` + `current_websocket is websocket` 守卫）、M3（非 dict 消息跳过）。

## 3. `_state.py` / `commands/__init__.py` / `help.py`（B/C/D）

| 目标 | 来源 | bytes | SHA256（落盘后） | src==dst |
|---|---|---|---|---|
| `scripts/commands/_state.py` | `patched/scripts/commands/_state.py` | 6056 | `521D3906A600D61C7A282141C04BA6897C878A32E7814E7319E5848725F6BC97` | ✅ |
| `scripts/commands/__init__.py` | `patched/scripts/commands/__init__.py` | 975 | `CF70807F952D6333D4A4CB80B931528D6A903748CCE5B5BEAE453F94F514EB70` | ✅ |
| `scripts/commands/help.py` | `patched/scripts/commands/help.py` | 4611 | `21D9BA1FEA800ABF391DE0A753CE3CAE956D98ED4A83655BE55B93E95C404107` | ✅ |

B = `_state.interaction_menus = None` + `init_handlers(..., menus=None)` 注入（**VERSION 仍为 2.5.0，未被上游 hunk 带回 2.4.0**）；C = `commands/__init__.py` 导入 `interaction`；D = `help.py` 追加『互动菜单』帮助行。

## 4. `group_manager.py` 改编（E1–E6 + S3）

| 目标 | 来源 | bytes | SHA256（落盘后） | src==dst |
|---|---|---|---|---|
| `scripts/group_manager.py` | `patched/scripts/group_manager.py` | 15454 | `DB7A8495FDC9AEE2BB0243BE174F3E6071AF99A614F9E79532729CB9E892FA4D` | ✅ |

新增 `poke_menu`/`reaction_menu` 两个功能位 + `FEATURE_DEFAULTS`（两者默认 **False**），并修正 `_normalize_entry` 不丢字段（`**entry`）、编辑旧群时不悄悄打开互动功能。

## 5. S1 本地补丁（`interaction_menu.py`）

| 目标 | 来源 | bytes | SHA256（落盘后） | src==dst |
|---|---|---|---|---|
| `scripts/interaction_menu.py`（**含 S1**） | `patched/`（= `stage/` + `diffs/S1__scripts__interaction_menu.py.diff`） | 17608 | `5D1E7321967BAF47BCBD993FDD02101CDADE9892FB1C0BD206CC5B5F19A60850` | ✅ |

S1 = `MenuConfig` 校验时拒绝 `values[2] == "互动菜单"`（防菜单自增殖）。注意落盘用的是 **17608 字节的 `patched/` 版本**，不是 `stage/` 的 17380 字节版。

## 6. 未改项（S2 / S4 / S5 与禁止项）

| 编号 | 内容 | 本波处置 |
|---|---|---|
| **S2** | `command` 允许 NUL 等控制字符（知情项，可选收紧） | **未改**（保持上游原样；REVIEW 判定「不改也可」） |
| **S4** | `poke_menu`/`reaction_menu` **不要**进 `_FEATURE_KEYS` | **已遵守**：`reverse_bot.py:66` 的 `_FEATURE_KEYS` 未新增这两个键（实测 `('decision_engine','plus_one','commands','newcomer_welcome','profile_record','vision','search')`） |
| **S5** | `_prune()` 饱和扫描 2938 µs/次（观测项） | **未改**（观测项，建议先不改） |
| **S6** | 配置错误的用户可见性 | **未改**（未列入本波必改） |
| 禁止项 | `scripts/command_handler.py` | **未改**：哈希仍为 `EE82E9B7…DAD1E`，mtime 仍为 2026-07-26T11:01:03 |
| 禁止项 | `config/group_features.json` | **未改**：哈希仍为 `822B1D7E…CD760`，mtime 仍为 2026-10-13T07:17:45 |
| 禁止项 | `NapCat/**`、`.env`、`web_search.py`、`search_gate.py`、`config/search.json`、`decision_engine.py`、`llm_pool.py`、`image_utils.py`、`image_cache.py`、`vision.py` | **全部未碰**（mtime 均早于 16:55 落盘时刻） |
| 禁止项 | 重启进程 / 启用任何群 / git 写操作 | **均未做**（见 §8） |

### 6.1 并行 search 工作流差异（如实标注来源，非本波越界）

`reverse_bot.py` 的 `_FEATURE_KEYS` 含 `"search"`、`config/group_features.json` 两个群各含 `"search": true` —— 均为**并行 search 工作流**所为，**不是本波改动**。
本波对 `reverse_bot.py` 的拷入是**纯增量**：落盘前已实测 `patched/reverse_bot.py == 生产 reverse_bot.py + PR#1 hunks`（见 §1 说明），故 `search` 相关代码**逐字保留**，未被回退。

### 6.2 运维注意（预先存在的分歧，非本波引入）

`group_manager.py` 的 `FEATURES` 表只有 **7 项**（6 旧 + `poke_menu` + `reaction_menu`，**不含 `vision`/`search`**），而 `reverse_bot._FEATURE_KEYS` 有 7 项（含 `vision`/`search`）。两表**本就不一致**（`vision`/`search` 从未进过 CLI 表）。
**已实测证明管理员用 CLI 编辑群不会吃掉这两个键**：`_normalize_entry` 用 `merged = dict(feat)` 起手并 `**entry` 展开，故 `vision`/`search` 原样保留，只补 `poke_menu`/`reaction_menu` 为 `False`。实测输出见 §7-CMD12。

---

## 7. 自检实测输出（全部在**生产树** `E:\QQbot` 上真跑，原文摘录）

> **路径正确性先证**：`check_facade.py` 解析脚本上三级 = `E:\QQbot`（恒为生产树）；`check_wiring.py`/`check_s1.py` 优先取 cwd，cwd=`E:\QQbot` 且 `scripts/interaction_menu.py` 存在 ⇒ root=生产树。**无任何校验脚本指向 `_pr1_stage/verify/` 隔离副本，无需改写。** 由 §7-CMD13 用模块 `__file__` 逐一点名反证。

### CMD1 `import reverse_bot`
```
OK
exit=0
```

### CMD2 `python -m unittest scripts.test_interaction_menu -v`
```
...
Ran 25 tests in 2.227s

OK
exit=0
```
（25/25 全过；`GroupConfigTests` 1 + `InteractionTests` 22 + `RPCTests` 2）

### CMD3 `scripts/test_llm_pool.py` → `结果: 全部通过` `exit=0`
### CMD4 `scripts/test_pool_harden.py` → `结果: 全部通过` `exit=0`（H1–H6 全 PASS）

### CMD5 `_state.VERSION`
```
VERSION 2.5.0
exit=0
```

### CMD6 `check_facade.py` —— 门面 11 符号
```
  handle_command         True
  process_voice_message  True
  _mystery_queue         True
  _dedup_lock            True
  BOT_QQ                 True
  VERSION                True
  init_handlers          True
  processed_msg_ids      True
  PROCESSED_MSG_MAX      True
  known_groups           True
  find_valid_number      True
FACADE_SYMBOLS=11/11  missing=none
exit=0
```

### CMD7 `check_registry.py`
```
registered = 33
dupes = []
has_menucmd = True
last_spec = '互动菜单'
last_match_type = exact
REGISTRY_OK = True
exit=0
```

### CMD8 `check_wiring.py` —— A1–A5 接线（生产树）
```
  [PASS] A2: reverse_bot.interaction_menus 是 InteractionMenus 实例
  [PASS] A2: 实例的 get_features 就是 reverse_bot._get_group_features
  [PASS] A2: 实例的 handle_command 就是 command_handler.handle_command
  [PASS] A3: rpc.resolve 可用（回执拦截器存在）
  [PASS] A5+B2: init_handlers(menus=...) 注入到 _state.interaction_menus
  [PASS] 默认全关: 未配置群 enabled() == False
  [PASS] C1: 指令『互动菜单』已注册
  [PASS] C1: 指令注册表无重名 (registered=33)
  [PASS] B: VERSION 未被上游 hunk 带回 2.4.0（当前 2.5.0）
  [PASS] S4: _state 未引入新功能位全局
  [PASS] S4: _FEATURE_KEYS 仍为 7 项且不含 poke_menu/reaction_menu
WIRING_FAILURES=0
exit=0
```
> 末行的「7 项」在并行 search 工作流之后语义已变（原为 6 旧项，现为 **6 旧项 + `search` = 7**，恰好仍等于 7，故断言未失效）。真正的末界（不含 `poke_menu`/`reaction_menu`）由该行与 §7-CMD13 双重确认。

### CMD9 `check_s1.py` —— S1 本地补丁（生产树）
```
  [PASS] S1 拒绝: command = 互动菜单                  accepted=False (期望 False)
  [PASS] S1 允许: command = 签到                    accepted=True (期望 True)
  [PASS] S1 允许: command = 互动菜单帮助                accepted=True (期望 True)
  [PASS] S1 允许: command = @bot 互动菜单             accepted=True (期望 True)
S1_FAILURES=0
exit=0
```

### CMD10 `scripts/group_manager.py`（feed `0`）
```
========== 群功能管理 ==========
...
选择: 再见。
exit=0
```
功能表元数据实测：`count 7`，`poke_menu | 戳一戳菜单 | default= False`、`reaction_menu | 表情交互 | default= False`（其余 5 项 `default= True`）。

### CMD12 group_manager 不丢键实测
```
轮前: {'284517232': ['commands','decision_engine','newcomer_welcome','plus_one','profile_record','search','vision'], ...}
轮后: {'284517232': ['commands','decision_engine','newcomer_welcome','plus_one','poke_menu','profile_record','reaction_menu','search','vision'], ...}
 群 284517232 | 丢失键: 无 | 新增键: ['poke_menu', 'reaction_menu']
RESULT: vision/search 保留；poke_menu/reaction_menu 补为 False（未启用）
```

### CMD13 `qq_native_re/findings/pr1/check_prod_proof.py`（新写·生产树来源反证 + 末界）
```
=== 1. 模块来源（必须是 E:\QQbot 生产树） ===
  reverse_bot                      E:\QQbot\reverse_bot.py
  scripts.interaction_menu         E:\QQbot\scripts\interaction_menu.py
  scripts.commands._state          E:\QQbot\scripts\commands\_state.py
  scripts.commands.interaction     E:\QQbot\scripts\commands\interaction.py
  scripts.command_handler          E:\QQbot\scripts\command_handler.py
  （5/5 PASS：均来自生产树，非 _pr1_stage/verify）
=== 2. 落盘内容不变量 ===
  _FEATURE_KEYS = ('decision_engine','plus_one','commands','newcomer_welcome','profile_record','vision','search')
  [PASS] 并行 search 工作流的 'search' 键仍在（未被本波回退）
  [PASS] _FEATURE_KEYS 不含 poke_menu / reaction_menu（末界）
  [PASS] _FEATURE_KEYS == 7 项（6 旧 + search）
  [PASS] _state.VERSION == 2.5.0
  [PASS] 门面再导出 × 11
=== 3. 默认全关 ===
  [PASS] 未配置群 enabled() == False
=== 4. group_features.json 末界（内容级） ===
  284517232 碧油鸡乐土（转正版） -> ['commands','decision_engine','newcomer_welcome','plus_one','profile_record','search','vision']
  [PASS] 群 284517232 未启用 poke_menu / reaction_menu
  [PASS] 群 284517232 的 search=true（并行工作流所为，非本波）
  1055881417 碧油鸡乐土(二阶英灵殿） -> [...同上...]
  [PASS] 群 1055881417 未启用 poke_menu / reaction_menu
=== 5. config/interaction_menu.json 语法 ===
  [PASS] interaction_menu.json 结构齐备
  [PASS] groups 为空 ⇒ 无任何群被启用
PROD_PROOF_FAILURES=0
exit=0
```

**生产树校验小结：全部绿灯**（import ✅ / 25 项离线测试 ✅ / 门面 11 符号 ✅ / registry 33 无重名 ✅ / 接线 A1–A5 ✅ / S1 ✅ / 池套件 ×2 ✅ / VERSION 2.5.0 ✅ / 默认全关 ✅ / `_FEATURE_KEYS` 不含菜单两键 ✅）。

---

## 8. 未越界证据（末界）

### 8.1 进程级
| 项 | 落盘前（16:47:37） | 收尾后（17:00） | 判定 |
|---|---|---|---|
| `:8080` LISTENING Owner PID | 45892 | **45892** | ✅ 未重启 |
| PID 45892 StartTime | 2026/10/13 8:41:58 | 2026/10/13 8:41:58 | ✅ 同一进程 |

### 8.2 禁止写文件（哈希 + mtime 双证，mtime 全部早于 16:55 落盘时刻）
```
scripts\command_handler.py      2586  2026-07-26T11:01:03  EE82E9B7AAE99F1A…   ← 红线，未变
config\group_features.json       596  2026-10-13T07:17:45  822B1D7E021ED830…   ← 未变
scripts\web_search.py          42153  2026-10-13T08:33:23  68B9401B66A0CA71…   ← 未碰
scripts\search_gate.py         15912  2026-10-13T12:08:04  981B93E17CA9E461…   ← 未碰
config\search.json               362  2026-10-13T08:37:10  3E142794847FFE12…   ← 未碰
scripts\decision_engine.py     71530  2026-10-13T08:07:30  F85E35279DFFF0A1…   ← 未碰
scripts\llm_pool.py            44731  2026-10-12T11:08:05  A00860AE24975431…   ← 未碰
.env                             698  2026-10-12T07:45:51  48B87D7A535778EF…   ← 未碰
NapCat\ 目录                    —     2026-06-06T10:55:36  —                   ← 未碰
.git\ 目录                      —     2026-10-13T15:20:23  —                   ← 无 git 写操作（早于 16:55）
```

### 8.3 内容级末界
- 群 `284517232` / `1055881417` 的 `features` 中**没有** `poke_menu` / `reaction_menu`（两键既不在 JSON 里，也不在 `interaction_menu.json` 的 `groups` 里 —— 后者为 `{}`）。
- `config/interaction_menu.json` 的 `groups` 为 `{}` ⇒ **零个群启用**互动菜单。
- 未启用任何群、未重启任何进程、未做任何 git 写操作。

### 8.4 本波实际写入清单（9 个生产文件，均在允许清单内）
`reverse_bot.py`、`scripts/interaction_menu.py`(新)、`scripts/commands/interaction.py`(新)、`scripts/test_interaction_menu.py`(新)、`config/interaction_menu.json`(新)、`scripts/commands/_state.py`、`scripts/commands/__init__.py`、`scripts/commands/help.py`、`scripts/group_manager.py`。
辅助写入（允许清单内）：`qq_native_re/patches/pr1_land_backup_20261013-165506/**`、`qq_native_re/findings/pr1/**`、`qq_native_re/_pr1_stage/tmp_diffcheck.py`。

---

## 9. 下一批实机验收操作步骤

> 前置：**先重启 bot**（当前 PID 45892 跑的是旧代码）。重启前确认 NapCat 4.18.38 在线。

### 步骤 0 — 重启并确认
```powershell
cd E:\QQbot && $env:PYTHONIOENCODING="utf-8"
# Ctrl-C 停掉旧进程，然后：
& ".\venv\Scripts\python" reverse_bot.py
```
确认日志出现 `:8080` 监听、NapCat 已连入；记录**新的** PID 作为本批基线。

### 步骤 1 — 关态回归（先证"默认全关"不打扰现有群）
在两群都**未启用**菜单时：
1. 桌面端 / 手机端**戳一戳**机器人 → **不应有任何菜单/回复**；
2. 手打 `@bot 互动菜单` → 应回复「本群尚未启用互动菜单，请管理员在群功能管理中开启。」；
3. `@bot 帮助` → 列表**应出现**新行「互动菜单 — @bot 互动菜单，或戳机器人后点击表情（需管理员按群启用）」；
4. 既有指令回归：`@bot 签到`、`@bot 今天吃啥`、`@bot 帮助`、识图各来一次 → 行为与落盘前一致。

### 步骤 2 — 只在群 `1055881417` 启用（先单群灰度）
```powershell
& ".\venv\Scripts\python" scripts\group_manager.py
# 选 2（编辑群功能）→ 群 1055881417 → 打开「指令响应」「戳一戳菜单」「表情交互」三个
```
> **`commands` 是硬前置**：`poke_menu`/`reaction_menu` 只有在 `commands` 也开时才生效。

### 步骤 3 — 开态功能验收（仅 1055881417）
1. **戳一戳**：戳机器人 → 收到带表情的菜单（👍 签到 / 🍜 今天吃啥 / ❔ 帮助）；
2. **点表情**：点 👍 → 应执行「签到」；点 ❔ → 应回帮助；
3. **不串群**：切到 `284517232` 再戳 → 仍**无**任何反应（白名单隔离）；
4. **S1 防自增殖**：把 `config/interaction_menu.json` 里某菜单项的 `command` 改成 `互动菜单` → 该配置应被**校验拒绝**（日志提示"command 不能指向互动菜单自身"），菜单不出现自指项；
5. **冷却/并发**：连续快速戳 → 30s 内只出一次菜单；同一人连点 → 3s 动作冷却生效、不排队。

### 步骤 4 — 断线重连
断开 NapCat 再恢复（或重启 NapCat）：
- 重连后戳一戳**仍正常**；
- 旧连接遗留的菜单/在飞任务被清掉，**迟到回执被丢弃**（不污染消息链）；
- 日志无 `Task exception was never retrieved` / `pending` 泄漏。

### 步骤 5 — 识图回归（`vision` 未在本波触碰，做冒烟即可）
在 `284517232` 发一张图 + `@bot 描述这张图` → 正常返回描述。

### 步骤 6 — 全量启用（可选，验收通过后）
在 `284517232` 同样打开三个开关，重复步骤 3。

---

## 10. 回滚路径

**备份目录**：`qq_native_re/patches/pr1_land_backup_20261013-165506/`（内含落盘前 5 个接线文件 + `command_handler.py` + `group_features.json` 的原样副本，以及 `PRE_STATE.txt` / `POST_STATE.txt` 哈希台账）。

### A. 只下线功能（最轻，无需改代码）
`python scripts\group_manager.py` → 把目标群的 `戳一戳菜单`、`表情交互` 关掉（或全关）。因为默认全关 + 白名单语义，**代码留着不生效**。

### B. 代码回滚（恢复 5 个接线文件）
```
copy qq_native_re\patches\pr1_land_backup_20261013-165506\reverse_bot.py                reverse_bot.py
copy qq_native_re\patches\pr1_land_backup_20261013-165506\scripts__group_manager.py     scripts\group_manager.py
copy qq_native_re\patches\pr1_land_backup_20261013-165506\scripts__commands___state.py  scripts\commands\_state.py
copy qq_native_re\patches\pr1_land_backup_20261013-165506\scripts__commands____init__.py scripts\commands\__init__.py
copy qq_native_re\patches\pr1_land_backup_20261013-165506\scripts__commands__help.py    scripts\commands\help.py
```
> ⚠️ **`reverse_bot.py` 回滚会回退并行 search 工作流的内容**（备份取自 16:55，其中 `_FEATURE_KEYS` 已含 `search`，故 **search 部分安全**；但若 search 工作流在 16:55 之后又改了 `reverse_bot.py`，本回滚会覆盖那些新改动）。**回滚前先重新备份当时的 `reverse_bot.py`**，或用 `_pr1_stage/patched/reverse_bot.py` 反向摘除 A1–A5 五个 hunk 而非整文件覆盖。

### C. 删除 4 个新增文件（彻底回滚时）
```
del scripts\interaction_menu.py
del scripts\commands\interaction.py
del scripts\test_interaction_menu.py
del config\interaction_menu.json
```
> 顺序建议：先做 B 再做 C（B 之后 `commands/__init__.py` 不再 import `interaction`，删文件才安全）。`scripts/commands/_state.py` 的 `menus=None` 参数可留着（默认 `None`，无副作用）。

### D. 回滚后自检
```powershell
& ".\venv\Scripts\python" -c "import reverse_bot; print('OK')"
& ".\venv\Scripts\python" qq_native_re\_pr1_stage\check_facade.py   # 期望 11/11
```
并核对 `config/group_features.json` 哈希回到 `822B1D7E021ED830E7618EB01CD903D7C044E3EA192F020000D33FDB123CD760`、`scripts/command_handler.py` 回到 `EE82E9B7AAE99F1A54A1BEB03AB9B12551E28EA6A758C4951D63A4FDE48DAD1E`。

---

## 11. 实机验收记录（2026-10-13 17:45–17:50）

- 重启后 bot 新 PID **46328**（17:45:38），`:8080` LISTENING + NapCat ESTABLISHED。
- NapCat 一手日志证据：17:46:27 与 17:48:12 各 3× `set_msg_emoji_like`（菜单预置回应）；17:48:11 菜单消息发出（`【互动菜单】全群可用，操作记在点击者本人名下。`）；**17:48:15 点击者回应触发原签到指令，并以点击者身份 @ 其本人**。
- 回归：`欢迎新人`/`[定向回复]`/`[防复读]`/`搜索资料=0条` 均正常。
- 详见 `qq_native_re/decisions/013_pr1_accepted.md`。
