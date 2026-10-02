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
```

也可以用 `python3 -m tx_indexer ...`。

筛选选项（两类命令通用，不同条件之间取交集；时间窗左闭右闭）：

- `--address ADDR`：精确匹配发送方或接收方（不可与 `--from-address` / `--to-address` 并用）
- `--from-address ADDR`：精确匹配发送方，可重复出现，集合内任一命中
- `--to-address ADDR`：精确匹配接收方，可重复出现，集合内任一命中
- `--method METHOD`：精确匹配方法，可重复出现，集合内任一命中
- `--start-time TS` / `--end-time TS`：非负 UTC 秒整数，含端点

集合类选项（`--from-address` / `--to-address` / `--method`）重复给定相同值等同一个条件。筛选值为空或仅含空白、或 `--address` 与付款方/收款方筛选并用，会在读取数据文件前报 `invalid_filter`。

`query` 额外选项：

- `--page-size N`：每页条数，默认 `100`，范围 1..1000
- `--cursor TOKEN`：上一页返回的 `next_cursor`

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

## 错误处理

领域错误输出到 stderr（单行 JSON，含 `error`、`message`、`input_line`），退出码为 `2`。只有输入数据行错误才带 1 起始行号，其余错误 `input_line` 为 `null`。

| error | 触发条件 |
| --- | --- |
| `invalid_transaction` | 记录解析或字段校验失败（含行号） |
| `duplicate_transaction` | `tx_hash` 冲突（含冲突所在行号） |
| `invalid_time_range` | 时间窗倒置（`start_time > end_time`） |
| `invalid_page_size` | `page_size` 越界或无法解析 |
| `invalid_cursor` | 游标非法（格式/解码错误）或与当前筛选不匹配 |
| `invalid_filter` | 筛选值为空白，或 `--address` 与 `--from-address` / `--to-address` 并用（读取数据文件前报错） |

## 代码结构

- `tx_indexer/loader.py`：JSON Lines 解析与校验
- `tx_indexer/engine.py`：筛选、排序、keyset 游标分页、聚合
- `tx_indexer/cursor.py`：不透明游标编解码（base64url）
- `tx_indexer/errors.py`：六类异常
- `tx_indexer/cli.py`：命令行入口
- `tests/`：unittest 测试（`python3 -m unittest discover -s tests`）

## 状态

已实现：公开查询、游标分页、聚合统计与六类异常；`--from-address` / `--to-address` / 可重复 `--method` 组合筛选。
