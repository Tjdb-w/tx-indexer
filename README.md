# Tx Indexer

链上交易索引与查询引擎：按地址、方法、时间窗建立索引，支持聚合统计与分页游标查询。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现，无外部依赖、无额外持久化，数据从 JSON Lines 文件读入内存。

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

## 增量导入与断点续传

`tx_indexer.importer.IncrementalImporter` 在查询索引之上提供增量交易导入：
持续接收按区块高度排列的批次，单个批次要么完整写入、要么完整拒绝，
同一位置重试得到可重复的结果。导入与查询共享同一份内存存储，导入返回
成功后查询与聚合立即观察到本批全部交易；已有查询、聚合与分页游标行为
不受影响。

```python
from tx_indexer.importer import IncrementalImporter

importer = IncrementalImporter()
result = importer.import_batch(batch)            # 首个批次
result = importer.import_batch(batch, cursor=result["next_import_cursor"])
indexer = importer.indexer                        # 底层 TxIndexer，可直接查询
```

批次结构（未定义的额外字段被忽略）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `chain_id` | 非空字符串 | 链标识；首个批次确定后不可变更 |
| `start_block` | 非负整数 | 本批（区块）高度 |
| `block_hash` | 非空字符串 | 本批区块哈希 |
| `transactions` | 数组 | 该区块内的交易列表，按原始顺序 |

每笔交易至少包含：`tx_hash`、`block_number`、`block_hash`、`timestamp`、
`from_address`、`to_address`、`method`、`amount`、`fee`、`success`。
其中 `block_number` 必须等于批次 `start_block`、`block_hash` 必须等于
批次 `block_hash`；`amount` / `fee` 为非负十进制整数字符串，
`success` 为布尔值。

导入成功返回（机器可读字段稳定）：

```json
{
  "status": "ok",
  "imported_count": 2,
  "skipped_count": 1,
  "confirmed_block_height": 10,
  "confirmed_block_hash": "0xabc",
  "next_import_cursor": "eyJ..."
}
```

续接规则：首个批次不要求游标；此后每次导入必须携带上一批返回的
`next_import_cursor`，且 `start_block` 为已确认高度 +1（新区块），或
指向已导入的高度（同一位置重试，要求区块哈希与交易集合完全一致）。
同一交易哈希再次出现且区块、时间、地址、方法、数值、费用、成功状态
完全一致时视为重试：跳过、不覆盖已存在数据、不重复计数或累计聚合。
导入游标只绑定链标识、已确认高度与区块哈希，与分页游标相互独立，
混用两边都会被拒绝。

导入拒绝时不产生任何新增索引数据，返回：

```json
{"status": "rejected", "error_code": "TX_CONFLICT", "message": "..."}
```

| error_code | 触发条件 |
| --- | --- |
| `INVALID_IMPORT_BATCH` | 批次缺少链标识、区块高度、区块哈希或交易必填字段，或字段类型非法、交易与批次声明的区块不一致 |
| `IMPORT_CURSOR_MISMATCH` | 游标缺失、非法或与当前已确认状态不一致；起始区块高度无法无缝续接（跳高度、未知旧高度、链标识不同） |
| `TX_CONFLICT` | 交易哈希已存在且任一决定查询结果的字段不同 |
| `BLOCK_CONFLICT` | 已导入高度上的区块哈希不同，或同一区块哈希对应的交易集合发生变化 |

### 链重组替换（replace_from）

链重组时，已确认的高高度区块可能需要整段替换。`replace_from(start_block, blocks, cursor)` 在**已导入的某一高度**上原子覆盖从该高度到链尖的整个旧后缀，同时保留更低高度的前缀：先完整校验新后缀，全部通过后才替换，任何失败都不改变索引。

```python
result = importer.replace_from(
    start_block,      # 已导入的高度
    blocks,           # 非空 import_batch 批次数组（新后缀）
    cursor,           # 当前 next_import_cursor
)
```

`blocks` 是普通 import_batch 批次（含 `chain_id`、`start_block`、`block_hash`、`transactions`，交易字段规则相同）组成的非空数组：首个批次的 `start_block` 必须等于 `start_block` 参数，区块高度连续（+1）、`chain_id` 一致，每笔交易的 `block_number`、`block_hash` 与其所属批次一致。新后缀可以比旧后缀更短（链尖回缩），也可以更长。

冲突与计数口径：

- 新后缀的 `tx_hash` 与**保留前缀**或**新后缀内部**的同名交易在任一决定字段（区块、时间、地址、方法、金额、费用、成功状态）上不一致 → `TX_CONFLICT`，整次替换拒绝、索引不变。
- **旧后缀**中的同名交易一律先替换、**不参与冲突判定**：决定字段完全相同的旧记录原样保留并计入 `skipped_count`；字段不同的旧记录删除后写入新记录；新后缀不再包含的旧记录直接删除。
- 新后缀内部完全相同的重复项同样跳过并计入 `skipped_count`。

成功返回（计数字段区分「删除的旧区块/旧交易」「写入的新区块/新交易」四种数量，确认字段给出新链尖）：

```json
{
  "status": "ok",
  "removed_block_count": 2,
  "removed_transaction_count": 2,
  "imported_block_count": 2,
  "imported_count": 2,
  "skipped_count": 0,
  "confirmed_block_height": 3,
  "confirmed_block_hash": "0xc3",
  "next_import_cursor": "eyJ..."
}
```

成功后 `importer.indexer` 的 query 与全部统计入口只观察新数据，旧后缀数据不再可见。返回的 `next_import_cursor` 绑定新链状态，可续用于 `import_batch`（在新链尖 +1 处追加）或再次 `replace_from`，且与分页游标相互独立、不能混用。

| error_code | 触发条件 |
| --- | --- |
| `INVALID_REPLACEMENT_BATCH` | `blocks` 为空或不是数组；区块结构非法、高度不连续、`chain_id` 不一致，或交易与所属区块的关系非法 |
| `IMPORT_CURSOR_MISMATCH` | `start_block` 不是已导入高度；游标缺失/非法/不是当前导入游标，或误用了分页游标；替换批次 `chain_id` 与已导入链不同 |
| `TX_CONFLICT` | 新后缀交易与保留前缀或新后缀内部同名交易的决定字段不一致 |

## 索引水位与幂等重放

`tx_indexer.replay.ReplayManager` 是一个独立可用的索引水位与幂等重放
入口，与现有查询、聚合统计和分页游标共享同一份内存索引：按链维护已
提交水位，让同一段链上历史可以被安全地重复提交，也能在网络中断后从
确定位置继续。处理顺序固定为从起始区块升序到结束区块，每个批次先
校验并标准化交易，再把交易写入现有索引，最后推进该链的已提交水位。

```python
from tx_indexer.replay import ReplayManager

def fetch_blocks(start_block, end_block):
    # 闭区间拉取；返回区块列表，顺序任意（按 block_number 归位）。
    # 暂时无法返回指定区块时抛 SourceUnavailableError。
    return [
        {"block_number": h, "transactions": [...]}
        for h in range(start_block, end_block + 1)
    ]

manager = ReplayManager(fetch_blocks)
result = manager.submit("chain-a", 0, 99, batch_size=10)
status = manager.status("chain-a")
indexer = manager.indexer             # 底层 TxIndexer，提交成功后立即可查
```

上游交易至少包含 `tx_hash`、`block_number`、`timestamp`、
`from_address`、`to_address`、`method`、`amount`（字段规则与数据文件
相同：`amount` 为非负十进制整数字符串；未定义的额外字段被忽略）。
`block_number` 必须等于交易所在区块的高度。标准化结果保留交易哈希、
区块高度、区块时间、发起地址、接收地址和方法标识（`amount` 作为
查询字段一并透传），写入后已有查询字段及排序口径不改变。

参数校验（在拉取任何区块之前）：起始区块大于结束区块、起始区块或
结束区块小于零、批量大小不在 1 到 1000 范围、链标识为空白时抛
`ValueError`。

### submit 返回

成功返回机器可读结果：

```json
{
  "status": "ok",
  "chain_id": "chain-a",
  "range_start": 0,
  "range_end": 99,
  "committed_start_block": 0,
  "committed_end_block": 99,
  "next_block": 100,
  "processed_batch_count": 10,
  "committed_count": 100,
  "skipped_count": 0,
  "last_batch_committed_at": 1791140000.0
}
```

- 范围与已提交水位重叠时，已覆盖部分不重新拉取，只从下一待处理区块
  续跑；整个范围都已被水位覆盖时直接返回幂等结果
  （`processed_batch_count` / `committed_count` / `skipped_count`
  均为 `0`）。
- `committed_count` 为本范围真正新增的交易数；`skipped_count` 为
  重新拉取到的批次中同哈希同内容的重复交易数；
  `last_batch_committed_at` 为该链最近成功批次时间（UTC 秒浮点）。

### 幂等、冲突与断点续传

- 相同交易哈希与相同标准化内容（区块高度、区块时间、发起地址、接收
  地址、方法标识）再次出现时视为重复：不新增记录，也不改变首次写入
  结果，计入 `skipped_count`。
- 相同交易哈希却出现不同区块高度、时间、地址或方法标识时抛
  `TransactionConflictError`，停止当前批次，水位**不**推进到冲突交易
  所在批次；同批中冲突交易之前已校验的交易也不可见。
- 批次只在整批成功后提交（先完整校验标准化，再整批写入并推进水位），
  中途失败不会留下半批已更新、半批未更新的结果。
- 上游暂时无法返回指定区块时抛 `SourceUnavailableError`（fetch 自身
  抛出，或返回列表缺少区间内任一区块）。已完整提交的前序批次和水位
  保持有效；网络恢复后用原参数原样重放整个范围即可——已覆盖部分幂等
  跳过，从未完成批次继续，最终结果与一次性成功完全一致。
- 不允许越过尚未提交的低区块形成更高水位：如已提交到 5 却直接提交
  `[7, 9]` 会抛 `ValueError`；不同链水位相互独立。

### status 只读状态入口

`status(chain_id, start_block=None)` 只返回水位、不扫描交易明细，也
不改变索引：

```json
{
  "chain_id": "chain-a",
  "committed_start_block": 0,
  "committed_end_block": 99,
  "next_block": 100,
  "last_batch_committed_at": 1791140000.0
}
```

尚未开始索引的链返回已提交区间为 `null`，`next_block` 等于本次传入
的 `start_block`；未传入 `start_block` 时返回 `null`（该链也从未配置
过起始区块）。`start_block` 为负整数时抛 `ValueError`。

### 并发语义

同一链的并发提交按区块顺序串行化：

- 相同范围并发提交合并为幂等结果：只有一个提交者真正执行批次，另一个
  等待其完成后返回同一水位结果；
- 不同范围不能越过尚未提交的低区块形成更高水位：后到的高范围等待
  前一低范围完成后继续；
- 任一提交失败（如冲突）都只影响其未提交批次，最终水位始终覆盖从
  已配置起始区块起的连续已提交区间；
- 不同链互不阻塞。

新功能启用后，单次查询、全部聚合统计与分页游标继续返回原有格式、
过滤语义、排序和兼容结果。

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

索引水位与幂等重放（`ReplayManager`）使用 Python 原生异常，不走 CLI
错误输出：

| 异常 | error 码 | 触发条件 |
| --- | --- | --- |
| `ValueError` | — | 起始区块大于结束区块、起止区块为负、批量大小不在 1..1000、链标识空白、交易/区块结构非法或越过未提交低区块 |
| `SourceUnavailableError` | `source_unavailable` | 上游暂时无法返回指定区块（fetch 抛出或返回缺少区间内区块） |
| `TransactionConflictError` | `transaction_conflict` | 相同交易哈希再次出现但区块高度、时间、地址或方法标识不同 |

## 代码结构

- `tx_indexer/loader.py`：JSON Lines 解析与校验
- `tx_indexer/engine.py`：筛选、排序、keyset 游标分页、聚合
- `tx_indexer/cursor.py`：不透明游标编解码（base64url），含独立的导入游标
- `tx_indexer/importer.py`：增量交易导入与断点续传
- `tx_indexer/replay.py`：索引水位与幂等重放（按链水位、原子批次、并发串行化）
- `tx_indexer/errors.py`：异常类型
- `tx_indexer/cli.py`：命令行入口
- `tests/`：unittest 测试（`python3 -m unittest discover -s tests`）

## 状态

已实现：公开查询、游标分页、聚合统计、按 method 分页汇总（method-stats）、按参与地址分页汇总（address-stats）、按交易对手分页汇总（counterparty-stats）、按固定宽度时间区间分页汇总（time-stats）、按有向交易对分页汇总（pair-stats）、按时间区间 × 参与地址分页汇总（address-time-stats）与领域异常；`--from-address` / `--to-address` / 可重复 `--method` 组合筛选；增量交易导入与断点续传（原子批次、重试判重、四类机器可读拒绝码、独立导入游标）；链重组后缀替换（`replace_from`，原子覆盖已导入高度至链尖、保留更低前缀、旧后缀同名不冲突、四类计数字段与 `INVALID_REPLACEMENT_BATCH` 拒绝码）；索引水位与幂等重放（`ReplayManager.submit` / `status`：按链连续水位、升序分批、整批原子提交、重复跳过、`TransactionConflictError` 冲突停止、`SourceUnavailableError` 断点续传、同链并发合并/等待、不扫描明细的只读状态入口）。
