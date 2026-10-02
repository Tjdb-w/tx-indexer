# Tx Indexer

链上交易索引与查询引擎：按地址、方法、时间窗建立索引，支持聚合统计与分页游标查询。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 输入格式

JSON Lines，每行一条交易，字段：

| 字段 | 约束 |
| --- | --- |
| `tx_hash` | 非空字符串，全文件唯一 |
| `block_number` | 非负整数 |
| `timestamp` | 非负整数（UTC 秒） |
| `from_address` / `to_address` | 非空字符串 |
| `method` | 非空字符串 |
| `amount` | 非负十进制整数字符串 |

## 用法

```bash
./tx-indexer query --file txs.jsonl [--address A] [--method M] \
    [--from-ts N] [--to-ts N] [--page-size N] [--cursor C]
./tx-indexer stats --file txs.jsonl [--address A] [--method M] \
    [--from-ts N] [--to-ts N]
```

也可用 `python3 tx_indexer.py ...` 调用；文件路径支持位置参数或 `--file`。

### 筛选

- `--address` 精确匹配发送方或接收方；`--method` 精确匹配方法名。
- 时间窗左闭右闭（`from_ts <= timestamp <= to_ts`）。
- 多个筛选条件取交集。

### query

输出 `{"transactions": [...], "total": N, "next_cursor": C|null}`：

- 交易按 `block_number` 升序，同高度按 `tx_hash` 升序。
- `total` 为全部匹配数（不限于当前页）。
- `page_size` 默认 100，合法范围 1–1000。
- `next_cursor` 取下一页，末页为 `null`；游标与筛选条件绑定，
  相同筛选下翻页不跳过、不重复、不乱序。

### stats

忽略分页，采用相同筛选，输出
`{"total_count", "total_amount", "min_amount", "max_amount", "avg_amount"}`：

- 金额为十进制整数字符串；`avg_amount` 向下取整。
- 无匹配时 `total_count` 为 0、`total_amount` 为 `"0"`，其余三项为 `null`。

## 错误

领域错误以 JSON 输出到 stderr，退出码 2，对象形如
`{"error": ..., "message": ..., "input_line": ...}`；
只有输入行错误带行号，其余 `input_line` 为 `null`。

| error | 异常 | 含义 |
| --- | --- | --- |
| `invalid_transaction` | `InvalidTransactionError` | 输入行解析或校验失败 |
| `duplicate_transaction` | `DuplicateTransactionError` | `tx_hash` 冲突 |
| `invalid_time_range` | `InvalidTimeRangeError` | 时间窗倒置或边界非法 |
| `invalid_page_size` | `InvalidPageSizeError` | `page_size` 越界 |
| `invalid_cursor` | `InvalidCursorError` | 游标非法或与筛选不匹配 |

## 测试

```bash
python3 -m unittest test_tx_indexer
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
