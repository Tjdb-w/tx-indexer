"""索引水位与幂等重放。

:class:`ReplayManager` 在现有查询索引（:class:`~tx_indexer.engine.TxIndexer`）
之上提供独立可用的增量索引治理：按链维护「已提交水位」，把同一段链上
历史安全地重复提交，并在网络中断后从确定位置继续。所有链共用同一份
查询索引，``tx_hash`` 是跨链全局身份。

:class:`MultiChainReplayManager` 提供多链共用入口的按链隔离视图：交易
身份（``(chain_id, tx_hash)``）、查询索引、已提交水位与查询游标全部按
``chain_id`` 隔离。相同 ``tx_hash`` 在不同链中属于不同交易，其区块
内容、冲突判定与查询结果互不可见；同链语义（升序分批、整批原子提交、
重放跳过、断点续传、同链并发串行化）与 :class:`ReplayManager` 一致。

公开入口：

- :meth:`ReplayManager.submit`：提交链标识、起始区块、结束区块与批量
  大小，处理顺序固定为从起始区块升序到结束区块；每个批次先校验并标准
  化交易，再整批写入现有索引，最后推进该链的已提交水位
- :meth:`ReplayManager.status`：只读索引状态，返回已提交起始区块、
  已提交结束区块、下一待处理区块与最近成功批次时间，不扫描交易明细、
  不改变索引
- :meth:`MultiChainReplayManager.query` /
  :meth:`MultiChainReplayManager.stats`：按链隔离的分页查询与聚合统计，
  筛选、排序与汇总口径与 :class:`~tx_indexer.engine.TxIndexer` 一致；
  分页游标额外绑定链标识

区块数据通过可调用对象 ``fetch_blocks(start, end)`` 按需拉取，返回
``{block_number, transactions}`` 映射的列表（顺序不要求，按高度归位）。
上游暂时无法返回指定区块时抛
:class:`~tx_indexer.errors.SourceUnavailableError`：已完整提交的前序
批次与水位保持有效，下一次原样重放未完成范围即可。

幂等与冲突：相同交易哈希与相同标准化内容（区块高度、区块时间、发起
地址、接收地址、方法标识）再次出现视为重复，不新增记录，也不改变首次
写入结果；相同交易哈希却出现不同内容时抛
:class:`~tx_indexer.errors.TransactionConflictError`，停止当前批次，
水位不推进到冲突交易所在批次。批次只在整批校验通过后提交，中途失败
不会留下半批结果。:class:`ReplayManager` 的身份在全部链之间共享，
:class:`MultiChainReplayManager` 的身份按链独立。

并发：同一链的提交按区块顺序串行化——相同范围并发提交合并为幂等
结果，后到的更高范围不能越过尚未提交的低区块形成更高水位，而是等待
前一范围完成后继续，最终水位始终覆盖连续已提交区间；不同链互不阻塞。
"""

import json
import threading
import time

from .cursor import (
    _b64url_decode,
    _canonical_filters,
    _encode_payload,
)
from .engine import DEFAULT_PAGE_SIZE, TxIndexer
from .errors import InvalidCursorError, SourceUnavailableError, TransactionConflictError
from .loader import _AMOUNT_RE, _is_nonneg_int, _is_nonempty_text

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 1000

#: 标准化后保留的交易身份与查询字段（顺序即写入索引的字段顺序）
_NORMALIZED_FIELDS = (
    "tx_hash",
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
    "amount",
)

#: 判定「同一交易哈希是否为同一笔交易」的标准化内容字段
_IDENTITY_FIELDS = (
    "block_number",
    "timestamp",
    "from_address",
    "to_address",
    "method",
)

#: MultiChainReplayManager 查询游标外层包装前缀（与普通分页游标互不通用）
_MC_CURSOR_PREFIX = "mcq1."


def _validate_range(start_block, end_block, batch_size):
    """提交参数校验：任一非法直接抛 ValueError。"""
    if not _is_nonneg_int(start_block):
        raise ValueError("start_block 必须为非负整数")
    if not _is_nonneg_int(end_block):
        raise ValueError("end_block 必须为非负整数")
    if start_block > end_block:
        raise ValueError(
            "start_block(%d) 不能大于 end_block(%d)"
            % (start_block, end_block)
        )
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < MIN_BATCH_SIZE
        or batch_size > MAX_BATCH_SIZE
    ):
        raise ValueError(
            "batch_size 必须为 %d 到 %d 之间的整数"
            % (MIN_BATCH_SIZE, MAX_BATCH_SIZE)
        )


def _normalize_transaction(raw, block_height, index):
    """校验并标准化一笔来自上游的交易。

    成功返回仅含固定字段的新字典（忽略未定义的额外字段）；任何结构或
    类型问题抛 ValueError。``block_number`` 必须等于其所在区块高度。
    """
    if not isinstance(raw, dict):
        raise ValueError(
            "区块 %d 的 transactions[%d] 必须是对象" % (block_height, index)
        )
    missing = [f for f in _NORMALIZED_FIELDS if f not in raw]
    if missing:
        raise ValueError(
            "区块 %d 的 transactions[%d] 缺少字段：%s"
            % (block_height, index, ", ".join(missing))
        )

    tx_hash = raw["tx_hash"]
    if not _is_nonempty_text(tx_hash):
        raise ValueError(
            "区块 %d 的 transactions[%d] tx_hash 必须为非空字符串"
            % (block_height, index)
        )
    block_number = raw["block_number"]
    if not _is_nonneg_int(block_number):
        raise ValueError(
            "区块 %d 的 transactions[%d] block_number 必须为非负整数"
            % (block_height, index)
        )
    if block_number != block_height:
        raise ValueError(
            "区块 %d 的 transactions[%d] block_number 为 %d，与所在区块不一致"
            % (block_height, index, block_number)
        )
    if not _is_nonneg_int(raw["timestamp"]):
        raise ValueError(
            "区块 %d 的 transactions[%d] timestamp 必须为非负 UTC 秒整数"
            % (block_height, index)
        )
    for name in ("from_address", "to_address", "method"):
        if not _is_nonempty_text(raw[name]):
            raise ValueError(
                "区块 %d 的 transactions[%d] %s 必须为非空字符串"
                % (block_height, index, name)
            )
    amount = raw["amount"]
    if not isinstance(amount, str) or _AMOUNT_RE.fullmatch(amount) is None:
        raise ValueError(
            "区块 %d 的 transactions[%d] amount 必须为非负十进制整数字符串"
            % (block_height, index)
        )

    return {name: raw[name] for name in _NORMALIZED_FIELDS}


def _same_identity(existing, tx):
    """同一 tx_hash 的标准化内容（区块、时间、地址、方法）是否一致。"""
    return all(existing[name] == tx[name] for name in _IDENTITY_FIELDS)


class _BaseReplayManager:
    """提交/水位/串行化的共用实现；存储作用域由子类决定。

    子类通过 :meth:`_new_chain` 与 :meth:`_chain_scope` 选择交易身份、
    索引与提交锁的隔离粒度：:class:`ReplayManager` 全部链共用一个作用域
    （跨链全局身份与共享索引），:class:`MultiChainReplayManager` 每条链
    一个独立作用域。
    """

    def __init__(self, fetch_blocks=None):
        if fetch_blocks is not None and not callable(fetch_blocks):
            raise ValueError("fetch_blocks 必须可调用")
        self._fetch_blocks = fetch_blocks
        # chain_id -> 链状态（首次提交时创建）
        self._chains = {}
        # 保护 _chains 的创建/取链
        self._registry_lock = threading.Lock()

    @staticmethod
    def _new_chain_state():
        return {
            # 该链已配置的起始区块（首次提交确定）
            "start": None,
            "committed_start": None,
            "committed_end": None,
            "last_batch_at": None,
            # 提交串行化条件变量；running 表示有提交正在执行
            "cv": threading.Condition(),
            "running": False,
        }

    def _new_chain(self):
        """创建一条新链的状态（子类可追加按链隔离的存储作用域）。"""
        return self._new_chain_state()

    def _chain_scope(self, chain):
        """返回某条链提交批次时使用的存储作用域（子类覆盖）。"""
        raise NotImplementedError

    def _get_chain(self, chain_id, create=False):
        if not create:
            return self._chains.get(chain_id)
        with self._registry_lock:
            chain = self._chains.get(chain_id)
            if chain is None:
                chain = self._new_chain()
                self._chains[chain_id] = chain
            return chain

    def submit(self, chain_id, start_block, end_block, batch_size,
               fetch_blocks=None):
        """提交 ``[start_block, end_block]``（闭区间、升序分批）并推进水位。

        每个批次先校验并标准化交易，再整批写入索引，最后推进已提交水位。
        已提交的重叠区块原样重放时按重复跳过（不新增、不改写）；上游
        不可用或交易冲突时，当前批次与后续批次不提交，前序已提交批次
        与水位保持有效。

        成功返回机器可读结果：

        ``{"status": "ok", "chain_id", "range_start", "range_end",
        "committed_start_block", "committed_end_block", "next_block",
        "processed_batch_count", "committed_count", "skipped_count",
        "last_batch_committed_at"}``

        参数非法抛 ValueError；上游不可用抛 SourceUnavailableError；
        同哈希内容冲突抛 TransactionConflictError。
        """
        _validate_range(start_block, end_block, batch_size)
        if not _is_nonempty_text(chain_id):
            raise ValueError("chain_id 必须为非空字符串")
        fetcher = fetch_blocks if fetch_blocks is not None else self._fetch_blocks
        if not callable(fetcher):
            raise ValueError("必须提供可调用的 fetch_blocks")

        chain = self._get_chain(chain_id, create=True)
        cv = chain["cv"]

        # 同一链的提交在此串行：相同范围合并等待，更低区块未提交时
        # 更高范围必须等待，绝不允许越过缺口形成更高水位。
        with cv:
            while True:
                committed_end = chain["committed_end"]
                committed_start = chain["committed_start"]

                # 已完全覆盖（含被相同范围的并发提交先行完成）：幂等返回
                if (
                    committed_end is not None
                    and start_block >= committed_start
                    and end_block <= committed_end
                ):
                    return self._ok_result(
                        chain_id, start_block, end_block,
                        committed_end=committed_end, batch_count=0,
                        committed=0, skipped=0, chain=chain,
                    )

                # 不允许提交低于该链已配置起始区块、又不与已提交区间
                # 相连的范围：水位只能从已配置起点连续向上延伸
                configured = chain["start"]
                if configured is not None and start_block < configured:
                    raise ValueError(
                        "链 %s 已配置起始区块为 %d，不能提交低于该起点的"
                        "范围 [%d, %d]"
                        % (chain_id, configured, start_block, end_block)
                    )

                # 本范围在已提交水位以内的部分直接视为重复，只处理续点
                effective_start = (
                    start_block if committed_end is None
                    else max(start_block, committed_end + 1)
                )

                # 缺口等待：续点必须紧接已提交水位，不允许越过尚未提交
                # 的低区块形成更高水位（首次提交即配置该链起始区块）
                configured_start = chain["start"]
                next_pending = (
                    start_block if configured_start is None
                    else (configured_start if committed_end is None
                          else committed_end + 1)
                )
                if chain["running"]:
                    # running：同链已有提交在执行（含相同范围的合并场景），
                    # 后到请求等待其完成后再按最新水位继续；等待期间对方
                    # 可能正好补上缺口，因此醒来后重新判定
                    cv.wait()
                    continue
                if effective_start > next_pending:
                    # 没有任何在途提交能填补缺口：不能越过尚未提交的低
                    # 区块形成更高水位
                    raise ValueError(
                        "链 %s 的范围 [%d, %d] 越过了尚未提交的低区块"
                        "（下一待处理区块为 %d）"
                        % (chain_id, start_block, end_block, next_pending)
                    )

                # 获得执行权即钉住该链已配置的起始区块：即使首个批次
                # 失败，后来的高范围也不能从中间起跳越过未提交的低区块
                if configured is None:
                    chain["start"] = start_block
                chain["running"] = True
                break

        try:
            # 拉取上游在锁外进行（可能很慢），不阻塞同链状态查询；
            # 批次的校验、写入、水位推进在 _run 内按批原子提交
            batch_count, committed, skipped = self._run(
                chain, effective_start, end_block, batch_size, fetcher
            )
        finally:
            with cv:
                chain["running"] = False
                cv.notify_all()

        return self._ok_result(
            chain_id, start_block, end_block,
            committed_end=chain["committed_end"],
            batch_count=batch_count, committed=committed,
            skipped=skipped, chain=chain,
        )

    def _run(self, chain, run_start, end_block, batch_size, fetcher):
        """升序执行批次（调用前已取得该链的执行权 ``running``）。

        返回 ``(执行批次数, 新增交易数, 重复跳过数)``。任何异常都只在
        完整批次边界生效：已成功的批次与水位保留，当前批不部分提交。
        """
        scope = self._chain_scope(chain)
        batch_count = 0
        committed_total = 0
        skipped_total = 0
        cursor = run_start
        while cursor <= end_block:
            batch_end = min(cursor + batch_size - 1, end_block)

            # 1. 拉取本批区块（不持有提交锁，避免不同链相互等待）；
            #    SourceUnavailableError 直接透出，此前批次已完整提交、
            #    水位有效，下次原样重放未完成范围即可
            blocks = fetcher(cursor, batch_end)
            if not isinstance(blocks, list) or not blocks:
                raise SourceUnavailableError(
                    "上游未返回区块区间 [%d, %d]" % (cursor, batch_end),
                    None,
                )
            by_height = {}
            for slot, block in enumerate(blocks):
                if not isinstance(block, dict):
                    raise ValueError(
                        "fetch_blocks 返回的 blocks[%d] 必须是对象" % slot
                    )
                height = block.get("block_number")
                if not _is_nonneg_int(height):
                    raise ValueError(
                        "fetch_blocks 返回的 blocks[%d] 缺少合法 block_number"
                        % slot
                    )
                raw_txs = block.get("transactions")
                if not isinstance(raw_txs, list):
                    raise ValueError(
                        "区块 %d 的 transactions 必须是数组" % height
                    )
                if height in by_height:
                    raise ValueError(
                        "fetch_blocks 对区块高度 %d 返回了多次" % height
                    )
                by_height[height] = raw_txs

            expected = set(range(cursor, batch_end + 1))
            missing = expected - set(by_height)
            if missing:
                raise SourceUnavailableError(
                    "上游暂时无法返回区块：%s"
                    % ", ".join(str(h) for h in sorted(missing)),
                    None,
                )
            extra = set(by_height) - expected
            if extra:
                raise ValueError(
                    "fetch_blocks 返回了请求区间外的区块：%s"
                    % ", ".join(str(h) for h in sorted(extra))
                )

            # 2/3. 提交锁内完成「校验标准化 → 重复/冲突判定 →
            #    整批写入 → 水位推进」，使交易台账、索引与链水位保持
            #    原子；锁的作用域即身份与索引的隔离粒度（共享全局或按链）。
            #    任何失败都发生在写入之前，不留半批结果
            with scope["commit_lock"]:
                ledger = scope["ledger"]
                normalized = []
                seen_in_batch = {}
                for height in range(cursor, batch_end + 1):
                    for index, raw in enumerate(by_height[height]):
                        tx = _normalize_transaction(raw, height, index)
                        tx_hash = tx["tx_hash"]
                        existing = ledger.get(tx_hash)
                        if existing is None:
                            existing = seen_in_batch.get(tx_hash)
                        if (
                            existing is not None
                            and not _same_identity(existing, tx)
                        ):
                            raise TransactionConflictError(
                                "tx_hash %s 已存在但标准化内容不一致"
                                % tx_hash,
                                None,
                            )
                        seen_in_batch.setdefault(tx_hash, tx)
                        normalized.append(tx)

                # 重复哈希跳过、不覆盖首次写入结果
                new_records = []
                added_hashes = set()
                for tx in normalized:
                    tx_hash = tx["tx_hash"]
                    if tx_hash in ledger or tx_hash in added_hashes:
                        # 与已提交记录同哈希同内容，或本批内部的多余副本：
                        # 计重复、只写一次
                        skipped_total += 1
                        continue
                    ledger[tx_hash] = tx
                    added_hashes.add(tx_hash)
                    new_records.append(tx)
                if new_records:
                    scope["indexer"].append_records(new_records)
                committed_total += len(new_records)

                # 4. 推进该链水位到本批末块，记录最近成功批次时间
                with chain["cv"]:
                    if chain["committed_end"] is None:
                        chain["start"] = cursor
                        chain["committed_start"] = cursor
                    chain["committed_end"] = batch_end
                    chain["last_batch_at"] = time.time()

            batch_count += 1
            cursor = batch_end + 1

        return batch_count, committed_total, skipped_total

    @staticmethod
    def _ok_result(chain_id, range_start, range_end, committed_end,
                   batch_count, committed, skipped, chain):
        committed_start = chain["committed_start"]
        return {
            "status": "ok",
            "chain_id": chain_id,
            "range_start": range_start,
            "range_end": range_end,
            "committed_start_block": committed_start,
            "committed_end_block": committed_end,
            "next_block": None if committed_end is None else committed_end + 1,
            "processed_batch_count": batch_count,
            "committed_count": committed,
            "skipped_count": skipped,
            "last_batch_committed_at": chain["last_batch_at"],
        }

    def status(self, chain_id, start_block=None):
        """只读返回某条链的索引水位状态，不扫描交易明细、不改变索引。

        返回 ``{"chain_id", "committed_start_block",
        "committed_end_block", "next_block", "last_batch_committed_at"}``。
        尚未开始索引的链已提交区间为 ``None``，``next_block`` 等于本次
        传入的 ``start_block``，未传入时返回 ``None``（该链也未配置过
        起始区块）；已配置（曾提交）但因并发暂不可见等情况下回退到该链
        已配置的起始区块。
        """
        if not _is_nonempty_text(chain_id):
            raise ValueError("chain_id 必须为非空字符串")
        if start_block is not None and not _is_nonneg_int(start_block):
            raise ValueError("start_block 必须为非负整数")

        chain = self._get_chain(chain_id)
        if chain is None:
            next_block = start_block
            return {
                "chain_id": chain_id,
                "committed_start_block": None,
                "committed_end_block": None,
                "next_block": next_block,
                "last_batch_committed_at": None,
            }

        with chain["cv"]:
            committed_end = chain["committed_end"]
            if committed_end is None:
                # 尚未提交：优先用调用方给的起点，否则用已配置起点
                next_block = (
                    start_block if start_block is not None else chain["start"]
                )
                return {
                    "chain_id": chain_id,
                    "committed_start_block": None,
                    "committed_end_block": None,
                    "next_block": next_block,
                    "last_batch_committed_at": None,
                }
            return {
                "chain_id": chain_id,
                "committed_start_block": chain["committed_start"],
                "committed_end_block": committed_end,
                "next_block": committed_end + 1,
                "last_batch_committed_at": chain["last_batch_at"],
            }


class ReplayManager(_BaseReplayManager):
    """按链维护索引水位、支持幂等重放的提交管理器。

    通过 :attr:`indexer` 暴露底层 :class:`~tx_indexer.engine.TxIndexer`：
    批次提交成功后，现有查询、聚合统计与分页游标立即观察到本批全部交易，
    格式与语义不变。全部链共用这一份索引与同一份 ``tx_hash`` 台账：
    不同链提交相同哈希、相同标准化内容的交易按重复跳过。

    上游区块由构造参数 ``fetch_blocks`` 提供：签名为
    ``fetch_blocks(start_block, end_block)``（闭区间），返回区块列表，
    每个区块为 ``{"block_number": int, "transactions": [...]}``；
    暂时无法返回指定区块时抛
    :class:`~tx_indexer.errors.SourceUnavailableError`。也可以在每次
    :meth:`submit` 时通过同名参数覆盖。
    """

    def __init__(self, fetch_blocks=None, indexer=None):
        super().__init__(fetch_blocks=fetch_blocks)
        self._indexer = indexer if indexer is not None else TxIndexer([])
        # 全部链共用的存储作用域：跨链全局 tx_hash 台账 + 共享索引 +
        # 保护台账判定与索引追加的跨链原子提交锁
        self._shared_scope = {
            "indexer": self._indexer,
            "ledger": {},
            "commit_lock": threading.Lock(),
        }

    @property
    def indexer(self):
        """底层查询索引；提交成功写入的交易立即可查。"""
        return self._indexer

    def _chain_scope(self, chain):
        return self._shared_scope


def _mc_wrap_cursor(chain_id, filters, inner_cursor):
    """把链内 query 游标包装为绑定 chain_id 与等价筛选的对外游标。"""
    payload = {
        "v": 1,
        "chain": chain_id,
        "f": _canonical_filters(filters),
        "cur": inner_cursor,
    }
    return _MC_CURSOR_PREFIX + _encode_payload(payload)


def _mc_unwrap_cursor(token, chain_id, filters):
    """校验并拆开 :class:`MultiChainReplayManager` 的 query 游标。

    游标必须由多链管理器的 query 签发，且绑定的链标识与筛选快照与本次
    请求一致；格式错误、跨链复用、跨命令复用（普通分页/导入游标）或
    筛选不等价都抛 :class:`~tx_indexer.errors.InvalidCursorError`。
    返回链内 :class:`~tx_indexer.engine.TxIndexer` 使用的内层游标。
    """
    if not isinstance(token, str) or not token.startswith(_MC_CURSOR_PREFIX):
        raise InvalidCursorError("游标不属于当前命令", None)

    raw = _b64url_decode(token[len(_MC_CURSOR_PREFIX):])
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("游标内容非法", None) from exc

    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise InvalidCursorError("游标版本不受支持", None)

    saved_chain = payload.get("chain")
    if not isinstance(saved_chain, str) or saved_chain.strip() == "":
        raise InvalidCursorError("游标链标识非法", None)
    if saved_chain != chain_id:
        raise InvalidCursorError("游标与当前链不匹配", None)

    saved_filters = payload.get("f")
    if not isinstance(saved_filters, dict):
        raise InvalidCursorError("游标缺少筛选信息", None)
    try:
        if _canonical_filters(saved_filters) != _canonical_filters(filters):
            raise InvalidCursorError("游标与当前筛选条件不匹配", None)
    except (TypeError, ValueError) as exc:
        raise InvalidCursorError("游标筛选信息非法", None) from exc

    inner = payload.get("cur")
    if not isinstance(inner, str) or inner == "":
        raise InvalidCursorError("游标位置信息非法", None)
    return inner


class MultiChainReplayManager(_BaseReplayManager):
    """多链共用入口、按链隔离身份/索引/水位/游标的重放管理器。

    与 :class:`ReplayManager` 的区别仅在隔离粒度：

    - 交易身份为 ``(chain_id, tx_hash)``：相同 ``tx_hash`` 在不同链中
      属于不同交易，同哈希但标准化内容不同也**不**跨链冲突；同链首次
      写入不覆盖，同链同哈希不同内容仍抛
      :class:`~tx_indexer.errors.TransactionConflictError`
    - 每条链拥有独立的 :class:`~tx_indexer.engine.TxIndexer`：查询与
      统计只观察本链提交的交易
    - 水位、缺口规则与同链并发串行化沿用 :meth:`ReplayManager.submit`，
      异链提交互不阻塞、互不影响
    - query 分页游标绑定 query 命令、``chain_id`` 与等价筛选，跨链复用、
      改换筛选或与其它命令游标混用都抛
      :class:`~tx_indexer.errors.InvalidCursorError`

    上游区块仍由构造参数 ``fetch_blocks`` 提供，并可在每次
    :meth:`submit` 时覆盖；两者都没有可调用函数时抛 ``ValueError``。
    """

    def __init__(self, fetch_blocks=None):
        super().__init__(fetch_blocks=fetch_blocks)

    def _new_chain(self):
        chain = self._new_chain_state()
        # 按链隔离的存储作用域：独立索引、独立 tx_hash 台账、独立提交锁
        chain["scope"] = {
            "indexer": TxIndexer([]),
            "ledger": {},
            "commit_lock": threading.Lock(),
        }
        return chain

    def _chain_scope(self, chain):
        return chain["scope"]

    def query(self, chain_id, filters, page_size=DEFAULT_PAGE_SIZE, cursor=None):
        """按链分页查询，筛选、排序与返回口径同
        :meth:`TxIndexer.query <tx_indexer.engine.TxIndexer.query>`。

        ``filters`` 为 :func:`~tx_indexer.engine.normalize_filters` 归一化
        后的筛选字典，时间窗左闭右闭。游标只在同一 ``chain_id`` 与等价
        筛选下可续页：跨链复用、改换筛选或使用其它命令的游标抛
        :class:`~tx_indexer.errors.InvalidCursorError`；``page_size``
        非法抛 :class:`~tx_indexer.errors.InvalidPageSizeError`。

        尚未开始索引的链返回
        ``{"transactions": [], "total": 0, "next_cursor": None}``，
        不创建链、不改变任何状态。
        """
        if not _is_nonempty_text(chain_id):
            raise ValueError("chain_id 必须为非空字符串")
        # page_size 校验先于链查找：未知链上非法页大小同样报错
        TxIndexer._validate_page_size(page_size)

        inner_cursor = None
        if cursor is not None:
            inner_cursor = _mc_unwrap_cursor(cursor, chain_id, filters)

        chain = self._get_chain(chain_id)
        if chain is None:
            return {"transactions": [], "total": 0, "next_cursor": None}

        result = chain["scope"]["indexer"].query(
            filters, page_size=page_size, cursor=inner_cursor
        )
        if result["next_cursor"] is not None:
            result["next_cursor"] = _mc_wrap_cursor(
                chain_id, filters, result["next_cursor"]
            )
        return result

    def stats(self, chain_id, filters):
        """按链聚合统计，汇总口径同
        :meth:`TxIndexer.stats <tx_indexer.engine.TxIndexer.stats>`。

        尚未开始索引的链返回无匹配结果（``total_count`` 为 0、
        ``total_amount`` 为 ``"0"``、金额字段为 ``None``），不创建链、
        不改变任何状态。
        """
        if not _is_nonempty_text(chain_id):
            raise ValueError("chain_id 必须为非空字符串")

        chain = self._get_chain(chain_id)
        if chain is None:
            return {
                "total_count": 0,
                "total_amount": "0",
                "min_amount": None,
                "max_amount": None,
                "avg_amount": None,
            }
        return chain["scope"]["indexer"].stats(filters)
