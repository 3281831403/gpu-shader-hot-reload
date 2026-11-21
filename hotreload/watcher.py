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

import copy
import hashlib

#: 切换后旧程序的默认宽限帧数：至少还要跨过一帧，不能在 commit 里立即释放。
GRACE_FRAMES = 2


class ReloadError(Exception):
    """热重载失败：编译失败、非法事件、非法帧数或状态不一致。"""


def _empty_state():
    """返回一个全新的空状态。"""
    return {"active": None, "retired": [], "released": []}


def _copy_state(state):
    """深拷贝状态字典，保证与入参不共享任何可变对象。

    缺失的键按空状态补全，便于处理外部传入的不完整状态。
    """
    if not isinstance(state, dict):
        return _empty_state()

    result = _empty_state()
    result["active"] = copy.deepcopy(state.get("active"))
    result["retired"] = [
        {
            "program": copy.deepcopy(entry.get("program")),
            "refs": entry.get("refs", 0),
        }
        for entry in (state.get("retired") or [])
    ]
    result["released"] = list(state.get("released") or [])
    return result


def _switch(state, candidate, grace_frames):
    """在深拷贝后的状态上完成一次原子切换。"""
    new_state = _copy_state(state)
    old_program = new_state["active"]
    if old_program is not None:
        # 旧程序整体退场，按宽限帧数延迟释放，绝不立即丢弃。
        new_state["retired"].append({"program": old_program, "refs": grace_frames})
    # 新 active 是候选的深拷贝：文件集与哈希都来自同一个候选，不会新旧混用。
    new_state["active"] = copy.deepcopy(candidate)
    return new_state


def _has_duplicate_paths(program):
    """判断一个程序的 files 里是否存在重复 path。"""
    if not isinstance(program, dict):
        return False
    seen = set()
    for item in program.get("files") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if path in seen:
            return True
        seen.add(path)
    return False


def content_hash(files):
    """返回一组着色器文件的内容哈希（十六进制 SHA-256 摘要）。

    契约：
    - 哈希只由每个文件的 ``(path, content)`` 决定，与 ``files`` 的顺序无关；
    - 路径参与哈希：内容完全相同但路径不同（改名）时哈希必须不同；
    - ``mtime`` 不参与哈希，仅改 mtime 不改变哈希；
    - 空文件列表返回固定值 ``hashlib.sha256(b"").hexdigest()``。
    """
    digest = hashlib.sha256()
    records = []
    for item in files or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "")
        content = item.get("content", "")
        records.append((path, content))

    # 按 (path, content) 排序后做长度前缀编码，保证与输入顺序无关且不会因为
    # 路径/内容里含分隔符而产生歧义。路径参与摘要，所以改名必然改变哈希。
    for path, content in sorted(records, key=lambda pair: (str(pair[0]), str(pair[1]))):
        path_bytes = str(path).encode("utf-8")
        content_bytes = str(content).encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content_bytes).to_bytes(8, "big"))
        digest.update(content_bytes)

    return digest.hexdigest()


def detect_change(old, new):
    """比较两份文件列表，返回内容发生变化的文件路径（按路径升序）。

    契约：
    - 以内容哈希式的 ``(path, content)`` 比较为准，容差为 0（逐字节相等），
      ``mtime`` 不参与判断；
    - 同一秒内 mtime 未变但内容变了，必须检出，不允许因为粒度粗而漏检；
    - 新增文件、删除文件、内容修改都算变化；
    - 无变化时返回空列表。
    """

    def content_map(files):
        # path -> 该路径下所有内容（排序后），重复路径也按内容逐项比较。
        mapping = {}
        for item in files or []:
            if not isinstance(item, dict):
                continue
            mapping.setdefault(item.get("path"), []).append(item.get("content"))
        for values in mapping.values():
            values.sort()
        return mapping

    old_map = content_map(old)
    new_map = content_map(new)

    changed = []
    for path in set(old_map) | set(new_map):
        if old_map.get(path) != new_map.get(path):
            changed.append(path)

    return sorted(changed, key=str)


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
    if not isinstance(files, (list, tuple)) or not files:
        raise ReloadError("空文件列表")

    seen_paths = set()
    copies = []
    for item in files:
        if not isinstance(item, dict):
            raise ReloadError("文件记录非法")
        if "path" not in item or "content" not in item or "mtime" not in item:
            raise ReloadError("文件缺少必要字段")

        path = item["path"]
        content = item["content"]
        if not isinstance(path, str) or path == "":
            raise ReloadError("路径为空")
        if not isinstance(content, str):
            raise ReloadError("文件内容非法")
        if path in seen_paths:
            raise ReloadError("路径重复: %s" % path)
        seen_paths.add(path)
        # 模拟着色器语法错误：编译失败时不得产出候选。
        if "#error" in content:
            raise ReloadError("编译失败: %s" % path)

        copies.append(copy.deepcopy(item))

    copies.sort(key=lambda entry: entry["path"])
    return {"files": copies, "hash": content_hash(copies), "compiled": True}


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
    if not isinstance(candidate, dict) or candidate.get("compiled") is not True:
        raise ReloadError("候选未编译")

    return _switch(active, candidate, GRACE_FRAMES)


def rollback(active, snapshot):
    """放弃当前活动程序，把状态恢复到 ``snapshot``。

    契约：
    - 返回的状态与 ``snapshot`` 完全一致（active / retired / released 逐项相等），
      是深拷贝，不共享可变对象，也不残留失败程序；
    - 不修改 ``active`` 与 ``snapshot`` 两个入参。
    """
    # 深拷贝快照，既保证逐项相等，又不与快照共享任何可变对象。
    return copy.deepcopy(snapshot) if isinstance(snapshot, dict) else _empty_state()


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
    # 先校验再动状态：非法帧数不会留下任何改动。
    if not isinstance(frames, int) or frames < 0:
        raise ReloadError("非法帧数: %r" % (frames,))

    new_state = _copy_state(active)
    still_retired = []
    for entry in new_state["retired"]:
        remaining = entry.get("refs", 0) - frames
        if remaining > 0:
            still_retired.append({"program": entry.get("program"), "refs": remaining})
        else:
            # 引用归零才释放，并把 hash 记入 released。
            program = entry.get("program")
            released_hash = program.get("hash") if isinstance(program, dict) else None
            new_state["released"].append(released_hash)

    new_state["retired"] = still_retired
    return new_state


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
    problems = []
    state = active if isinstance(active, dict) else {}

    # 1. 候选必须已经编译成功。
    if pending is not None and (
        not isinstance(pending, dict) or pending.get("compiled") is not True
    ):
        problems.append("候选未编译")

    # 2. 引用计数已归零却仍留在 retired 里，属于句柄泄漏。
    retired = state.get("retired") or []
    if any(
        isinstance(entry, dict) and entry.get("refs", 0) <= 0 for entry in retired
    ):
        problems.append("引用未释放")

    # 3. 待提交候选或当前活动程序里存在重复路径。
    has_duplicate = _has_duplicate_paths(pending) or _has_duplicate_paths(
        state.get("active")
    )
    if has_duplicate:
        problems.append("路径重复")

    return problems


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
    grace_frames = config.get("grace_frames") if isinstance(config, dict) else None
    if not isinstance(grace_frames, int) or grace_frames < 0:
        raise ReloadError("非法 grace_frames: %r" % (grace_frames,))

    # 批处理从空状态开始；每个事件独立编译到自己的候选槽。
    state = _empty_state()
    applied = []
    try:
        for event in events:
            candidate = compile_stage(event)
            applied.append(candidate["hash"])
            state = _switch(state, candidate, grace_frames)
    except ReloadError as exc:
        # 任一事件失败：整批回滚，不留部分结果。
        return {
            "status": "rolled_back",
            "applied": [],
            "active": _empty_state(),
            "error": str(exc),
        }

    return {"status": "ok", "applied": applied, "active": state, "error": None}
