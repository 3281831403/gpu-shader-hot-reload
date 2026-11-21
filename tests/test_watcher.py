"""着色器热重载的 15 个回归测试（初始全部红）。"""

import pytest

from hotreload.watcher import (
    ReloadError,
    commit,
    compile_stage,
    content_hash,
    detect_change,
    drain_references,
    reload_batch,
    rollback,
    validate_state,
)

# hashlib.sha256(b"").hexdigest()：空文件列表的固定哈希。
EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def f(path, content, mtime=0.0):
    """构造一个文件记录。"""
    return {"path": path, "content": content, "mtime": mtime}


def program(path, content, mtime=0.0, hash_value="hash"):
    """构造一个已编译程序。"""
    return {"files": [f(path, content, mtime)], "hash": hash_value, "compiled": True}


def empty_state():
    return {"active": None, "retired": [], "released": []}


# ---------------------------------------------------------------- 10 个缺陷


def test_hash_includes_path_so_rename_triggers():
    """缺陷 1：哈希只算内容不算路径，改名不触发。"""
    old = [f("shaders/a.frag", "void main() {}", 100)]
    renamed = [f("shaders/b.frag", "void main() {}", 100)]
    assert content_hash(old) != content_hash(renamed)


def test_detect_change_uses_content_not_mtime():
    """缺陷 2：变化检测用 mtime，同秒修改漏检。"""
    old = [f("a.frag", "old", 100), f("b.frag", "same", 100)]
    # mtime 一模一样（同一秒内修改），只改内容，必须检出。
    same_tick = [f("a.frag", "new", 100), f("b.frag", "same", 100)]
    assert detect_change(old, same_tick) == ["a.frag"]
    # 只改 mtime、内容不变，不算变化。
    mtime_only = [f("a.frag", "old", 999), f("b.frag", "same", 999)]
    assert detect_change(old, mtime_only) == []


def test_compile_failure_never_replaces_active():
    """缺陷 3：编译失败也替换，画面全黑。"""
    active = {"active": compile_stage([f("a.frag", "ok", 1)]), "retired": [], "released": []}
    with pytest.raises(ReloadError):
        compile_stage([f("a.frag", "#error: missing ';'", 2)])
    # 未编译的候选不允许提交，活动程序保持原样。
    with pytest.raises(ReloadError):
        commit(active, None)
    assert active["active"]["hash"] == content_hash([f("a.frag", "ok", 1)])


def test_commit_switches_atomically_without_mixing():
    """缺陷 4：切换非原子，一帧内新旧混用。"""
    old_program = {"files": [f("a.frag", "v1", 1), f("b.frag", "v1", 1)], "hash": "old", "compiled": True}
    candidate = {"files": [f("a.frag", "v2", 2)], "hash": "new", "compiled": True}
    state = {"active": old_program, "retired": [], "released": []}

    swapped = commit(state, candidate)

    assert swapped["active"] is candidate or swapped["active"] == candidate
    assert swapped["active"]["hash"] == "new"
    assert swapped["active"]["files"] == candidate["files"]
    assert [item["path"] for item in swapped["active"]["files"]] == ["a.frag"]
    # 旧程序完整退场，不和新程序混在同一份 active 里。
    assert any(entry["program"]["hash"] == "old" for entry in swapped["retired"])


def test_old_program_is_released_lazily():
    """缺陷 5：旧程序立即释放，正在使用的帧崩溃。"""
    old_program = program("a.frag", "v1", 1, "old")
    candidate = program("a.frag", "v2", 2, "new")
    state = {"active": old_program, "retired": [], "released": []}

    swapped = commit(state, candidate)

    assert [entry["program"]["hash"] for entry in swapped["retired"]] == ["old"]
    assert swapped["released"] == []
    # 再推进一帧，旧程序仍在宽限期里，句柄有效。
    after_one = drain_references(swapped, 1)
    assert any(entry["program"]["hash"] == "old" for entry in after_one["retired"])
    # 宽限期结束后才允许释放。
    after_all = drain_references(after_one, 10)
    assert after_all["retired"] == []
    assert after_all["released"] == ["old"]


def test_rollback_restores_snapshot_exactly():
    """缺陷 6：回滚不恢复快照，状态丢失。"""
    snapshot = {
        "active": program("a.frag", "stable", 1, "stable"),
        "retired": [{"program": program("a.frag", "older", 0, "older"), "refs": 1}],
        "released": ["released-hash"],
    }
    broken = {"active": program("a.frag", "broken", 2, "broken"), "retired": [], "released": []}

    restored = rollback(broken, snapshot)

    assert restored == snapshot
    assert restored is not snapshot
    assert restored["active"]["hash"] == "stable"


def test_reference_drain_prevents_dangling_handles():
    """缺陷 7：引用计数不检查，句柄悬空。"""
    stale = program("a.frag", "old", 1, "old")
    state = {"active": None, "retired": [{"program": stale, "refs": 3}], "released": []}

    # 还有 1 帧没结束，不能释放。
    still_used = drain_references(state, 2)
    assert still_used["retired"][0]["refs"] == 1
    assert still_used["released"] == []

    # 引用归零却留在 retired 里，属于句柄泄漏，必须报出来。
    leaked = {"active": None, "retired": [{"program": stale, "refs": 0}], "released": []}
    assert validate_state(leaked, None) == ["引用未释放"]

    # 引用全部结束后才释放，状态恢复一致。
    drained = drain_references(state, 3)
    assert drained["retired"] == []
    assert drained["released"] == ["old"]
    assert validate_state(drained, None) == []


def test_duplicate_paths_rejected():
    """缺陷 8：重复路径不报错，重复编译。"""
    duplicated = [f("a.frag", "v1", 1), f("a.frag", "v2", 2)]
    with pytest.raises(ReloadError):
        compile_stage(duplicated)
    candidate = {"files": duplicated, "hash": "dup", "compiled": True}
    assert validate_state(empty_state(), candidate) == ["路径重复"]


def test_batch_events_use_independent_candidate_slots():
    """缺陷 9：批量共用候选槽，事件之间串。"""
    events = [
        [f("a.frag", "event-one", 1)],
        [f("b.frag", "event-two", 2)],
    ]

    result = reload_batch(events, {"grace_frames": 0})

    assert result["status"] == "ok"
    # 第一个事件的结果必须还是它自己的内容，不能被第二个事件覆盖。
    assert result["applied"][0] == content_hash(events[0])
    assert result["applied"][0] != content_hash(events[1])
    assert result["active"]["active"]["hash"] == content_hash(events[1])

    # 任一事件非法时整批回滚，前面的事件也不许留下结果。
    bad_batch = [events[0], [f("b.frag", "#error: bad", 2)]]
    rolled_back = reload_batch(bad_batch, {"grace_frames": 0})
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["applied"] == []
    assert rolled_back["active"]["active"] is None
    assert rolled_back["error"]


def test_drain_negative_frames_raises_reload_error():
    """缺陷 10：frames<0 不校验，死循环。"""
    state = {"active": program("a.frag", "v1", 1, "old"), "retired": [], "released": []}
    with pytest.raises(ReloadError):
        drain_references(state, -1)


# ---------------------------------------------------------------- 5 个组合


def test_identical_content_hashes_equal():
    """同内容哈希相同（顺序与 mtime 都不影响）。"""
    left = [f("a.frag", "same", 1), f("b.frag", "also", 2)]
    right = [f("b.frag", "also", 999), f("a.frag", "same", 42)]
    assert content_hash(left) == content_hash(right)


def test_changed_content_changes_hash():
    """内容变哈希变。"""
    before = [f("a.frag", "v1", 1)]
    after = [f("a.frag", "v2", 1)]
    assert content_hash(before) != content_hash(after)


def test_empty_file_list_has_fixed_hash():
    """空文件列表返回固定哈希。"""
    assert content_hash([]) == EMPTY_HASH


def test_negative_grace_frames_in_config_raise_reload_error():
    """frames<0 抛 ReloadError（批处理配置入口）。"""
    with pytest.raises(ReloadError):
        reload_batch([[f("a.frag", "v1", 1)]], {"grace_frames": -1})


def test_successful_batch_preserves_event_order():
    """批量全成功时顺序不变。"""
    events = [
        [f("a.frag", "one", 1)],
        [f("b.frag", "two", 2)],
        [f("c.frag", "three", 3)],
    ]

    result = reload_batch(events, {"grace_frames": 2})

    assert result["status"] == "ok"
    assert result["applied"] == [content_hash(event) for event in events]
    assert result["active"]["active"]["hash"] == content_hash(events[-1])
