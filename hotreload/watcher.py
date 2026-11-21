"""着色器热重载的核心契约（故意全部未实现，初始 15 个测试全红）。

数据模型
--------
文件::

    {"path": str, "content": str, "mtime": float | int}

程序（一次成功编译的产物）::

    {"files": [file, ...], "hash": str, "compiled": True}

活动状态::

    {
        "active":  程序 | None,
        "retired": [{"program": 程序, "refs": int}, ...],
        "released": [程序 hash, ...],
    }

``retired`` 里的旧程序按引用计数延迟释放，只有 ``refs`` 归零才允许进入
``released``；任何一帧都不允许拿到已经释放的句柄。

约束
----
- 只使用标准库（hashlib、time），不联网、不 sleep；
- 时间一律由调用方显式传入（mtime 字段），实现不得读取系统时钟做判断；
- 内容比较是零容差的（容差 0）：path 与 content 必须逐字节相等，mtime 不参与
  任何容差判断；
- 内容哈希与顺序无关，但必须包含路径。
"""

import hashlib

#: 切换后旧程序的默认宽限帧数：至少还要跨过一帧，不能在 commit 里立即释放。
GRACE_FRAMES = 2


class ReloadError(Exception):
    """热重载失败：编译失败、非法事件、非法帧数或状态不一致。"""


def content_hash(files):
    """返回一组着色器文件的内容哈希（十六进制 SHA-256 摘要）。

    契约：
    - 哈希只由每个文件的 ``(path, content)`` 决定，与 ``files`` 的顺序无关；
    - 路径参与哈希：内容完全相同但路径不同（改名）时哈希必须不同；
    - ``mtime`` 不参与哈希，仅改 mtime 不改变哈希；
    - 空文件列表返回固定值 ``hashlib.sha256(b"").hexdigest()``。
    """
    raise NotImplementedError


def detect_change(old, new):
    """比较两份文件列表，返回内容发生变化的文件路径（按路径升序）。

    契约：
    - 以内容哈希式的 ``(path, content)`` 比较为准，容差为 0（逐字节相等），
      ``mtime`` 不参与判断；
    - 同一秒内 mtime 未变但内容变了，必须检出，不允许因为粒度粗而漏检；
    - 新增文件、删除文件、内容修改都算变化；
    - 无变化时返回空列表。
    """
    raise NotImplementedError


def compile_stage(files):
    """把文件列表编译为候选程序，不触碰当前活动程序。

    成功时返回::

        {"files": 按 path 排序的文件副本, "hash": content_hash(files), "compiled": True}

    契约：
    - 任一文件缺少 ``path`` / ``content`` / ``mtime`` 键、路径为空、路径重复，
      或 ``content`` 含 ``"#error"``（模拟着色器语法错误）时，编译失败，抛
      :class:`ReloadError`；失败时不得产出候选，更不得改动任何活动程序；
    - ``files`` 为空视为非法，抛 :class:`ReloadError`；
    - 不修改入参。
    """
    raise NotImplementedError


def commit(active, candidate):
    """把候选程序原子地切换为活动程序，返回新的状态字典。

    ``active`` 是状态字典（见模块文档），``candidate`` 是 :func:`compile_stage`
    的返回值。

    契约：
    - 切换是原子的：返回状态里的 ``active`` 整体等于 ``candidate``，文件集、
      哈希都来自同一个候选，任何一帧都不允许看到新旧混用的文件；
    - 候选未编译（``None`` 或 ``compiled`` 不为 ``True``）时抛
      :class:`ReloadError`，状态原样保留；
    - 旧活动程序移入 ``retired``，``refs`` 取 :data:`GRACE_FRAMES`，延迟释放，
      绝不立即丢弃；
    - 不修改入参，返回新的状态字典。
    """
    raise NotImplementedError


def rollback(active, snapshot):
    """放弃当前活动程序，把状态恢复到 ``snapshot``。

    契约：
    - 返回的状态与 ``snapshot`` 完全一致（active / retired / released 逐项相等），
      是深拷贝，不共享可变对象，也不残留失败程序；
    - 不修改 ``active`` 与 ``snapshot`` 两个入参。
    """
    raise NotImplementedError


def drain_references(active, frames):
    """推进 ``frames`` 帧，释放引用计数归零的旧程序，返回新的状态字典。

    契约：
    - ``frames`` 必须是非负整数；``frames < 0``（含 bool 之外的非法类型）时抛
      :class:`ReloadError`，且不产生任何状态改动；
    - 每个 ``retired`` 项的 ``refs`` 减去 ``frames``；
    - ``refs <= 0`` 的项从 ``retired`` 移除并进入 ``released``（记录其 hash）；
    - 仍有引用的项保留在 ``retired``，句柄不悬空，不允许提前释放；
    - 不修改入参。
    """
    raise NotImplementedError


def validate_state(active, pending):
    """检查状态一致性，返回问题列表。

    ``pending`` 是待提交的候选程序，可为 ``None``。

    问题文案与返回顺序固定：
    1. ``"候选未编译"``：``pending`` 非 ``None`` 但未编译；
    2. ``"引用未释放"``：``retired`` 中存在 ``refs <= 0`` 却仍未被释放的项
       （句柄泄漏）；
    3. ``"路径重复"``：``pending`` 或 ``active["active"]`` 的 files 里 path 重复。

    没有问题时返回空列表。
    """
    raise NotImplementedError


def reload_batch(events, config):
    """按事件顺序批量重载：要么全部成功，要么整体回滚。

    ``events`` 是 ``list[list[file]]``，每个事件是一组要一起编译的文件；
    ``config`` 是 ``{"grace_frames": int}``。

    成功时返回::

        {"status": "ok", "applied": [每个事件的程序 hash, ...],
         "active": 最终状态, "error": None}

    失败时返回::

        {"status": "rolled_back", "applied": [], "active": 批处理开始前的空状态,
         "error": 失败原因}

    契约：
    - 事件之间使用彼此独立的候选槽：``applied[i]`` 必须等于第 i 个事件自身
      文件的哈希，后面的事件不得改写前面事件的结果；
    - 任一事件非法（编译失败、空事件、重复路径等）都必须整批回滚，不留部分
      结果；
    - ``config["grace_frames"]`` 必须是 ``>= 0`` 的整数，否则抛
      :class:`ReloadError`；
    - 不修改入参。
    """
    raise NotImplementedError
