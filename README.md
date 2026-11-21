# gpu-shader-hot-reload

## 题面

接入着色器热重载后，改错语法直接黑屏、改对又要重启引擎：内容哈希只算内容不算路径，改名不触发重载；变化检测用 mtime，同秒修改漏检；编译失败也替换，画面全黑；切换非原子，一帧内新旧混用；旧程序立即释放，正在使用的帧崩溃；回滚不恢复快照，状态丢失；引用计数不检查，句柄悬空；重复路径不报错，重复编译；批量共用候选槽，事件之间串；frames<0 不校验，死循环。hotreload/watcher.py 是核心，tests/test_watcher.py 15个红。要修的是：内容哈希含路径、按内容而非 mtime 检测变化、编译失败不替换、切换原子、旧程序延迟释放、回滚恢复快照、引用计数归零才释放、重复路径报错、批量候选槽隔离且任一事件非法时整体回滚、拒绝 frames<0。

## 运行方式

```bash
python -m pytest tests -q
```

初始状态下 `hotreload/watcher.py` 的 8 个函数全部 `raise NotImplementedError`，15 个测试全部调用这些桩，因此输出尾部恰好是 `15 failed`，且没有 collection / import / syntax error；把 8 个函数按 docstring 契约实现后，应变为 `15 passed`。全程离线，只用标准库（hashlib、time）与 pytest，测试不 sleep、不读系统时间，`mtime` 由测试显式传入。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 图形渲染 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## tests 不可修改

`tests/test_watcher.py` 的 15 个测试就是验收标准，禁止修改、删除、跳过或弱化断言；只能修改 `hotreload/watcher.py`（必要时可调整 `hotreload/__init__.py` 的导出）。判分只看 `python -m pytest tests -q` 的 15 个结果。
