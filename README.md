# Tx Indexer

链上交易索引与查询引擎：按地址、方法、时间窗建立索引，支持聚合统计与分页游标查询；支持按区块高度排列的增量交易导入与断点续传。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现，无外部依赖、无额外持久化，数据从 JSON Lines 文件或增量导入批次读入内存。

## 数据格式

每行一个 JSON 对象，字段固定为：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tx_hash` | 非空字符串 | 全文件唯一 |
| `block_number` | 非负整数 | 区块高度 |
| `timestamp` | 非负整数 | UTC 秒 |
| `from_address` / `to_address` / `method` | 非空字符串 | 发送方 / 接收方 / 方法 |
| `amount` | 非负十进制整数字符串 | 如 `"1000"`；不接受数字类型、负号、小数点 |

不允许缺少字段或存在未定义字段。空行也视为非法记录。

## 增量导入与断点续传

`tx_indexer.importer.IncrementalImporter` 在内存索引之上提供按区块高度排列的批次导入入口 `import_batch(batch, cursor=None)`；导入返回成功后，本批全部交易立即可被 `importer.indexer` 上的全部查询与聚合观察到，且既有查询、分页游标、聚合语义完全不变。

批次结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `chain_id` | 非空字符串 | 链标识；首次导入后固定，后续批次必须一致 |
| `block_number` | 非负整数 | 本批区块高度（每批一个区块） |
| `block_hash` | 非空字符串 | 该高度的区块哈希 |
| `transactions` | 列表 | 该区块内的交易（可为空） |

每笔交易至少包含：`tx_hash`、`block_number`、`block_hash`、`timestamp`、`from_address`、`to_address`、`method`、`amount`、`fee`、`success`。其中 `block_number` / `block_hash` 必须与批次一致；`amount` / `fee` 为非负十进制整数字符串；`success` 为布尔值；其余字段的校验口径与 JSON Lines 数据相同。多余字段被忽略。

导入结果始终为结构化字典，机器可读字段稳定。成功：

```json
{
  "status": "imported",
  "chain_id": "chain-a",
  "imported_count": 2,
  "skipped_count": 1,
  "confirmed_block_number": 11,
  "confirmed_block_hash": "0xabc",
  "next_import_cursor": "eyJ..."
}
```

拒绝（整批不产生任何索引数据）：

```json
{"status": "rejected", "error_code": "TX_CONFLICT", "message": "..."}
```

| error_code | 触发条件 |
| --- | --- |
| `INVALID_IMPORT_BATCH` | 批次缺少链标识、区块高度、区块哈希，或交易缺少必填字段、字段非法、交易区块字段与批次不一致 |
| `IMPORT_CURSOR_MISMATCH` | 批次高度无法从已确认高度无缝续接（出现空洞或回退到导入起点之前）、chain_id 与已导入链不一致，或给定的导入游标与当前导入状态不一致 |
| `BLOCK_CONFLICT` | 已确认高度上的区块哈希不同，或同一区块哈希对应的交易集合发生变化 |
| `TX_CONFLICT` | 同一 `tx_hash` 再次出现，但区块、时间、地址、方法、数值、费用或成功状态中任一字段不同 |

语义约定：

- 单个批次要么完整写入、要么完整拒绝；任何拒绝都不改动索引与导入状态，进程中途退出后重新导入也不会产生半个区块或重复聚合。
- 同一 `tx_hash` 再次出现且上述全部字段完全一致时视为重试：跳过、计入 `skipped_count`，不重复增加计数或聚合值，已存在数据不被覆盖。
- 批次高度必须等于已确认高度 + 1（新区块），或落在已导入区间内（幂等重试：内容完全一致时全部跳过并返回相同游标）。首个批次可从任意高度开始。
- `next_import_cursor` 为不透明令牌，绑定链标识、已确认高度、区块哈希与已索引交易数；与分页游标完全独立：导入游标用于分页查询会报 `invalid_cursor`，分页游标作为导入游标会被拒绝为 `IMPORT_CURSOR_MISMATCH`。
- 同一输入序列从空索引一次性导入与分批逐步导入，查询命中、聚合统计值与分页顺序完全一致。

## 命令行用法

无需安装，直接使用仓库根目录的可执行脚本（需要 Python 3）：

```bash
./tx-indexer query <data.jsonl> [选项]
./tx-indexer stats <data.jsonl> [选项]
./tx-indexer method-stats <data.jsonl> [选项]
./tx-indexer address-stats <data.jsonl> [选项]
./tx-indexer counterparty-stats <data.jsonl> --address ADDR [选项]
./tx-indexer time-stats <data.jsonl> --bucket-size SECONDS [选项]
./tx-indexer pair-stats <data.jsonl> [选项]
./tx-indexer address-time-stats <data.jsonl> --bucket-size SECONDS [选项]
```

也可以用 `python3 -m tx_indexer ...`。

筛选选项（两类命令通用，不同条件之间取交集；时间窗左闭右闭）：

- `--address ADDR`：精确匹配发送方或接收方（不可与 `--from-address` / `--to-address` 并用）
- `--from-address ADDR`：精确匹配发送方，可重复出现，集合内任一命中
- `--to-address ADDR`：精确匹配接收方，可重复出现，集合内任一命中
- `--method METHOD`：精确匹配方法，可重复出现，集合内任一命中
- `--start-time TS` / `--end-time TS`：非负 UTC 秒整数，含端点

集合类选项（`--from-address` / `--to-address` / `--method`）重复给定相同值等同一个条件。筛选值为空或仅含空白、或 `--address` 与付款方/收款方筛选并用，会在读取数据文件前报 `invalid_filter`。

`query`、`method-stats`、`address-stats`、`counterparty-stats`、`time-stats`、`pair-stats` 与 `address-time-stats` 额外选项：

- `--page-size N`：每页条数，默认 `100`，范围 1..1000
- `--cursor TOKEN`：上一页返回的 `next_cursor`

`time-stats` 与 `address-time-stats` 还必须给定：

- `--bucket-size SECONDS`：区间宽度（秒），大于 0 的整数；缺失、非整数或不大于 0 会在读取数据文件前报 `invalid_bucket_size`

### query 返回

交易按 `block_number` 升序、同高度按 `tx_hash` 升序排列：

```json
{
  "transactions": [ ... ],
  "total": 3,
  "next_cursor": "eyJ..."
}
```

- `total` 为全部匹配数（不是当前页条数）。
- `next_cursor` 指向下一页；末页为 `null`。游标不透明且自校验，相同筛选下翻页不会跳过、重复或乱序；改变筛选条件复用旧游标会报 `invalid_cursor`。

### stats 返回

忽略分页，使用与 query 相同的筛选；金额均为十进制整数字符串，`avg_amount` 向下取整：

```json
{
  "total_count": 2,
  "total_amount": "15",
  "min_amount": "5",
  "max_amount": "10",
  "avg_amount": "7"
}
```

无匹配时：`total_count` 为 `0`、`total_amount` 为 `"0"`，其余三项为 `null`。

### method-stats 返回

把匹配交易按 `method` 精确字符串值分组，金额为十进制整数字符串，`avg_amount` 向下取整。分组按 `total_amount` 数值降序、`total_count` 降序、`method` 的 Unicode 码点升序确定唯一顺序：

```json
{
  "groups": [
    {"method": "approve", "total_count": 1, "total_amount": "21", "avg_amount": "21"},
    {"method": "transfer", "total_count": 2, "total_amount": "15", "avg_amount": "7"}
  ],
  "total_groups": 2,
  "next_cursor": null
}
```

- `total_groups` 为全部分组数（不是当前页分组数）。
- 末页 `next_cursor` 为 `null`。游标不透明且自校验，只允许在相同命令及等价筛选条件下续用；跨命令复用（如把 query 游标用于 method-stats）或改变筛选条件都会报 `invalid_cursor`。
- 无匹配时：`groups` 为 `[]`、`total_groups` 为 `0`、`next_cursor` 为 `null`。

### address-stats 返回

把每条匹配交易按 `from_address`、`to_address` 的原字符串分别记一次发送、接收参与，并按地址聚合。自转账（from 与 to 相同）时：`total_count` 只计一次、发送/接收两个身份各加一、金额只累计一次。`send_count` / `receive_count` / `total_count` 分别统计该地址作为发送方、接收方、参与方的不同交易数；金额为十进制整数字符串，`avg_amount = total_amount // total_count` 向下取整。分组按 `total_amount` 数值降序、`total_count` 降序、`send_count` 降序、`receive_count` 降序、`address` 的 Unicode 码点升序确定唯一顺序：

```json
{
  "groups": [
    {"address": "alice", "send_count": 2, "receive_count": 1, "total_count": 3, "total_amount": "36", "avg_amount": "12"},
    {"address": "bob", "send_count": 1, "receive_count": 1, "total_count": 2, "total_amount": "31", "avg_amount": "15"},
    {"address": "carol", "send_count": 0, "receive_count": 1, "total_count": 1, "total_amount": "5", "avg_amount": "5"}
  ],
  "total_groups": 3,
  "next_cursor": null
}
```

- `total_groups` 为全部分组数（不是当前页分组数）。
- 末页 `next_cursor` 为 `null`。游标不透明且自校验，只允许在相同命令及等价筛选条件下续用；跨命令复用（如把 query/method-stats 游标用于 address-stats）或改变筛选条件都会报 `invalid_cursor`。
- 无匹配时：`groups` 为 `[]`、`total_groups` 为 `0`、`next_cursor` 为 `null`。

### counterparty-stats 返回

必须给定 `--address ADDR` 作为观察地址（缺失会在读取数据文件前报 `invalid_filter`）。只统计发送方或接收方为 ADDR 的匹配交易，且每笔交易只归一个对手：发送方为 ADDR 时归入 `to_address`，接收方为 ADDR 时归入 `from_address`；自转账以 ADDR 自身为对手，`total_count` 只计一次、`send_count` 与 `receive_count` 各加一、金额只累计一次。各分组的计数、金额、平均值口径与 address-stats 相同（从对手视角计发送/接收），仅将分组键 `address` 改名 `counterparty`。分组按 `total_amount` 数值降序、`total_count` 降序、`send_count` 降序、`receive_count` 降序、`counterparty` 的 Unicode 码点升序确定唯一顺序：

```json
{
  "address": "alice",
  "groups": [
    {"counterparty": "bob", "send_count": 1, "receive_count": 1, "total_count": 2, "total_amount": "31", "avg_amount": "15"},
    {"counterparty": "carol", "send_count": 0, "receive_count": 1, "total_count": 1, "total_amount": "5", "avg_amount": "5"}
  ],
  "total_groups": 2,
  "next_cursor": null
}
```

- `total_groups` 为全部对手数（不是当前页分组数）。
- 末页 `next_cursor` 为 `null`。游标不透明且自校验，不绑定 `page-size`，只允许在相同命令及等价筛选条件下续用；跨命令复用或改变观察地址、筛选条件都会报 `invalid_cursor`。
- 无匹配时：`address` 保留原值、`groups` 为 `[]`、`total_groups` 为 `0`、`next_cursor` 为 `null`。

### time-stats 返回

把匹配交易按固定宽度时间区间分桶：区间从 Unix 纪元对齐、左闭右开，`bucket_start = (timestamp // bucket_size) * bucket_size`，`bucket_end_exclusive = bucket_start + bucket_size`；每笔匹配交易恰好进入一个区间，时间窗端点与区间边界不漏计、不重复。只返回有交易的区间，按 `bucket_start` 升序分页。金额为十进制整数字符串，`avg_amount = total_amount // total_count` 向下取整：

```json
{
  "groups": [
    {"bucket_start": 0, "bucket_end_exclusive": 60, "total_count": 2, "total_amount": "31", "avg_amount": "15"},
    {"bucket_start": 120, "bucket_end_exclusive": 180, "total_count": 1, "total_amount": "5", "avg_amount": "5"}
  ],
  "total_groups": 2,
  "next_cursor": null
}
```

- `total_groups` 为全部非空区间数（不是当前页区间数）。
- 末页 `next_cursor` 为 `null`。游标不透明且自校验，绑定 time-stats、等价筛选与 `bucket_size`，不绑定 `page-size`；跨命令复用、改变筛选或 `bucket_size`、篡改或解码失败都会报 `invalid_cursor`。
- 无匹配时：`groups` 为 `[]`、`total_groups` 为 `0`、`next_cursor` 为 `null`。

### pair-stats 返回

把匹配交易按 `from_address` 到 `to_address` 的原字符串有向组合各归一组：`a→b` 与 `b→a` 是两个不同分组，自转账（from 与 to 相同）也累计一次。每组含 `total_count`、`total_amount`，金额为十进制整数字符串，`avg_amount = total_amount // total_count` 向下取整。分组按 `total_amount` 数值降序、`total_count` 降序、`from_address` 的 Unicode 码点升序、`to_address` 的 Unicode 码点升序确定唯一顺序：

```json
{
  "groups": [
    {"from_address": "bob", "to_address": "alice", "total_count": 1, "total_amount": "21", "avg_amount": "21"},
    {"from_address": "alice", "to_address": "bob", "total_count": 2, "total_amount": "15", "avg_amount": "7"}
  ],
  "total_groups": 2,
  "next_cursor": null
}
```

- `total_groups` 为全部有向组合数（不是当前页分组数）。
- 末页 `next_cursor` 为 `null`。游标不透明且自校验，只允许在相同命令及等价筛选条件下续用，不绑定 `page-size`；跨命令复用（如把其他命令的游标用于 pair-stats）、改变筛选条件、篡改或解码失败都会报 `invalid_cursor`。
- 无匹配时：`groups` 为 `[]`、`total_groups` 为 `0`、`next_cursor` 为 `null`。

### address-time-stats 返回

在 time-stats 的时间区间内再按参与地址聚合，用于观察各地址在每个时间区间内的活动。区间从 Unix 纪元对齐、左闭右开：`bucket_start = (timestamp // bucket_size) * bucket_size`，`bucket_end_exclusive = bucket_start + bucket_size`；每笔匹配交易恰好进入一个区间。区间内按地址聚合的口径与 address-stats 相同：`from`/`to` 不同时，发送方所在组 `send_count` 加一，接收方所在组 `receive_count` 加一，两组的 `total_count` 各加一、金额各累计一次；自转账只进一个组，`send_count` / `receive_count` / `total_count` 各加一，金额只累计一次。只返回非空（区间, 地址）组，按 `bucket_start` 升序、`total_amount` 数值降序、`total_count` 降序、`send_count` 降序、`receive_count` 降序、`address` 的 Unicode 码点升序确定唯一顺序。金额为十进制整数字符串，`avg_amount = total_amount // total_count` 向下取整：

```json
{
  "groups": [
    {"address": "alice", "bucket_start": 0, "bucket_end_exclusive": 60, "send_count": 2, "receive_count": 1, "total_count": 3, "total_amount": "36", "avg_amount": "12"},
    {"address": "bob", "bucket_start": 0, "bucket_end_exclusive": 60, "send_count": 1, "receive_count": 1, "total_count": 2, "total_amount": "31", "avg_amount": "15"},
    {"address": "carol", "bucket_start": 0, "bucket_end_exclusive": 60, "send_count": 0, "receive_count": 1, "total_count": 1, "total_amount": "5", "avg_amount": "5"}
  ],
  "total_groups": 3,
  "next_cursor": null
}
```

- `total_groups` 为全部非空（区间, 地址）组数（不是当前页组数）。
- 末页 `next_cursor` 为 `null`。游标不透明且自校验，绑定 address-time-stats、等价筛选与 `bucket_size`，不绑定 `page-size`；跨命令复用、改变筛选或 `bucket_size`、篡改或解码失败都会报 `invalid_cursor`。
- 无匹配时：`groups` 为 `[]`、`total_groups` 为 `0`、`next_cursor` 为 `null`。

## 错误处理

领域错误输出到 stderr（单行 JSON，含 `error`、`message`、`input_line`），退出码为 `2`。只有输入数据行错误才带 1 起始行号，其余错误 `input_line` 为 `null`。

| error | 触发条件 |
| --- | --- |
| `invalid_transaction` | 记录解析或字段校验失败（含行号） |
| `duplicate_transaction` | `tx_hash` 冲突（含冲突所在行号） |
| `invalid_time_range` | 时间窗倒置（`start_time > end_time`） |
| `invalid_page_size` | `page_size` 越界或无法解析 |
| `invalid_cursor` | 游标非法（格式/解码错误）或与当前筛选不匹配 |
| `invalid_filter` | 筛选值为空白、`--address` 与 `--from-address` / `--to-address` 并用，或 counterparty-stats 缺少 `--address`（读取数据文件前报错） |
| `invalid_bucket_size` | `--bucket-size` 缺失、非整数或不大于 0（time-stats / address-time-stats，读取数据文件前报错） |

## 代码结构

- `tx_indexer/loader.py`：JSON Lines 解析与校验
- `tx_indexer/engine.py`：筛选、排序、keyset 游标分页、聚合
- `tx_indexer/importer.py`：增量批次导入、幂等重试、导入游标与断点续传
- `tx_indexer/cursor.py`：不透明游标编解码（base64url）
- `tx_indexer/errors.py`：七类异常
- `tx_indexer/cli.py`：命令行入口
- `tests/`：unittest 测试（`python3 -m unittest discover -s tests`）

## 状态

已实现：公开查询、游标分页、聚合统计、按 method 分页汇总（method-stats）、按参与地址分页汇总（address-stats）、按交易对手分页汇总（counterparty-stats）、按固定宽度时间区间分页汇总（time-stats）、按有向交易对分页汇总（pair-stats）、按时间区间 × 参与地址分页汇总（address-time-stats）与七类异常；`--from-address` / `--to-address` / 可重复 `--method` 组合筛选；按区块高度排列的增量批次导入、幂等重试与断点续传（导入游标与分页游标相互独立）。
