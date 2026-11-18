# P1 R1 实施记录

日期：2026-11-17。分支：`feat/harness-p1-resume`。BASE：`2929550e`（#22 head）。

## 实现

- 每个工具步先持久化含 `pending_calls` 的检查点 A，再调用 Gateway；本步结果齐全后持久化空 `pending_calls` 的检查点 B。两次都更新任务的 `checkpoint_ref`。
- `ResumingContextBuilder` 先调用原 builder 获取当前 system、工具权限、约束和预算，再恢复检查点历史。缺失产物、无效 JSON 或 Pydantic 校验失败转为带 ref 的 `CheckpointUnreadable`，由已有 Worker 提交 `failed(internal)`。
- 新 Attempt 在首次模型调用前重放原动作键；部分等待保留原调用索引。重放遇到 unknown / needs_approval 继续等待；`ActionConflict` 原样抛出。新模型步仍依据 `model_call` 事件数编号。
- 检查点使用已有 `stage` 字段保存收尾阶段的工具禁用状态，避免崩溃恢复后执行原本应拒绝的调用。
- 内存和 PostgreSQL 增加 `reclaim_expired`：取消优先，其次未决副作用等待对账，再按尝试上限失败，其他回到 queued。结束 Attempt、清空当前 Attempt、状态事件在同一临界区 / 事务内完成；不重置用量、预留、deadline 或产物。
- PostgreSQL 使用 `FOR UPDATE SKIP LOCKED` 锁任务和对应 Attempt，并按 scope / task 排序。`Recovery.tick(now)` 只调用回收；`max_attempts` 默认 3，读取 `DARAOS_HARNESS_MAX_ATTEMPTS`，必须 ≥ 1。

只修改任务卡允许的实现位置。未修改 Gateway、Worker、TaskService、原 ContextBuilder、API、schema、迁移、Scheduler 或 #26 / #23 逻辑；没有连接常驻回收进程。

## 测试先行

各部分先新增任务卡对应的必测，再改相应实现，合计覆盖 13 项必测。初次运行因接口不存在而不能导入，记录的原始结果行：

```text
E   ModuleNotFoundError: No module named 'harness.tasks.recovery'
1 error in 3.78s

E   ModuleNotFoundError: No module named 'harness.context.resume'
1 error in 1.84s

E   ModuleNotFoundError: No module named 'harness.context.resume'
1 error in 14.12s
```

额外的收尾阶段恢复回归用例在修复前复现 connector 执行 1 次（断言应为 0 次），结果 `1 failed in 14.38s`。随后修改 Runner，不放宽断言。开发中也修正了新测试使用假 profile 配真实 instructions loader 的夹具组合；没有改动已有测试。

定向验证记录：上下文 / 配置 7 项、内存与 PostgreSQL 回收 23 项、Runner 新旧测试 43 项。下面完整测试命令的输出是最终交付依据。

## 提交前四项检查

最终测试使用 WSL Ubuntu 22.04 上的 Python 3.11 虚拟环境（pytest 9.2.0、Pydantic 2.12.5、SQLAlchemy 2.0.48、asyncpg 0.32.0）；pytest 的 `pythonpath=src` 指向本 worktree。PG fixture 自建并销毁临时 PostgreSQL 16 容器，未使用用户数据库。Git、grep、awk 检查在 Windows 执行；grep / awk 为 Git for Windows 自带程序，表达式与任务卡相同。

首轮使用 Windows 挂载盘上的 `<venv>/bin/python`：完整测试 476 通过、2 个跨进程 SSE 测试读取超时；单独重跑仍失败。未改动的 `2929550e` worktree 用同一环境重跑同一测试对也失败，原始汇总为：

```text
FAILED tests/harness_runtime/integration/test_processes.py::test_api_and_worker_share_tasks_across_processes[foreground]
FAILED tests/harness_runtime/integration/test_processes.py::test_api_and_worker_share_tasks_across_processes[background]
2 failed, 5 deselected in 64.46s (0:01:04)
```

该环境仅导入 Worker 与 PostgreSQL 模块即约 8–9 秒，测试 HTTP 读取超时为 10 秒，SSE keepalive 为 15 秒；它还装有超出仓库 `<2.1` 约束的 SQLAlchemy 2.1.6。因此改用上述已有 Linux 文件系统环境重新验证，没有调整测试超时、断言或 API。性能测量与测试有重叠，不能单独归因于挂载盘；基线对照证明旧环境失败并非仅在 R1 出现。

四项最终原始输出附在下文。

## 边界与后续

- R1 的回收尚未接进常驻进程，按卡留给 S1；没有执行 R2、S1、P1 统一验收或 R4。
- R1 是本地实现与测试交付，没有 push、没有开 PR，也没有把 #22 / #26 重新合入当前分支。
- R3 预检发现待对账任务的枚举接口缺失。R1 完成提交后从此分支最终提交切出 R3，只记录停止原因，不在 R1 中补越界接口。
- 已在组内开发记录中追加 P1 表格并读回核对。

## 四项检查原始输出

代码提交：`63e556d43e9ca7270f03eccbc006b82e46a85968`。本报告随后单独提交；报告提交不改变被测代码。以下历史输出中的环境路径统一替换为 `<venv>`、`<tmp>` 和 `<stdlib>`，测试数字和结论保持不变。

### 1. 完整测试

```bash
python -m pytest tests/harness_runtime -q -n0 -o addopts=""
```

最终运行使用 `<venv>/bin/python`，退出码 0。原始输出（包括现有 pytest-asyncio 提示）：

```text
<venv>/lib/python3.11/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
........................................................................ [ 15%]
........................................................................ [ 30%]
........................................................................ [ 45%]
........................................................................ [ 60%]
........................................................................ [ 75%]
........................................................................ [ 90%]
..............................................                           [100%]
478 passed in 46.00s

```

### 2. 防御性写法检查

```bash
git diff 2929550e -- src/harness | grep -nE '^\+.*(except Exception|except:|getattr\([^)]*,[^)]*,|hasattr\(|self\._\w+ is not None)|^\+\s+\w+=None,$|^\+.*def [^(]*\(.*=None[,)]|^\+ {8,}(from \.|import )'
```

原始输出为空；grep 退出码 1 表示没有匹配行。

```text
```

### 3. src/ 新增行数

```bash
git diff --numstat 2929550e -- src/ | awk '{s+=$1} END {print s}'
```

在所有新增文件已暂存后执行，退出码 0：

```text
210
```

上限 350；计数包含新建的 `context/resume.py` 和 `tasks/recovery.py`。

### 4. 与 #26 试合并

```bash
git fetch origin pull/26/head:refs/remotes/origin/pr-26 && git merge-tree --write-tree --name-only origin/pr-26 HEAD
```

#26 取回的 head 为 `800d2e1d4a62314b627cc682114067bebb8ebd17`。代码提交前先用已暂存 tree 创建无分支候选 commit `adc438d690a53e72e79c60d513aa2b41c8524435`，对该候选试合并；确认后提交代码，再对真实 HEAD 执行上面命令。两次退出码均为 0、输出相同；没有冲突文件，没有修改 worktree 或分支内容。fetch 无输出，merge-tree 原始输出：

```text
cdd1f39de9807e80ff38677b1239795ca4073d3f

```

附加范围检查：加入报告前的 `git diff --check` 输出为空。报告原样保留失败堆栈中的行尾空格，因此完整文档 diff 会提示这些原始输出行的 trailing whitespace；源代码与测试没有该问题。除允许的 9 个源文件、4 个新测试文件和本报告外，没有其他改动。

### 首轮完整测试失败记录（保留原始输出）

同一测试命令在旧挂载盘环境中退出码 1。之后没有修改源代码或测试，只更换为已有、依赖符合仓库约束的测试环境。旧环境的定向重跑及基线对照原始文件保存在 `R1-process-recheck.txt`、`BASE-process-check.txt`。

```text
..................FF.................................................... [ 15%]
........................................................................ [ 30%]
........................................................................ [ 45%]
........................................................................ [ 60%]
........................................................................ [ 75%]
........................................................................ [ 90%]
..............................................                           [100%]
=================================== FAILURES ===================================
_________ test_api_and_worker_share_tasks_across_processes[foreground] _________

    @contextlib.contextmanager
    def map_httpcore_exceptions() -> typing.Iterator[None]:
        global HTTPCORE_EXC_MAP
        if len(HTTPCORE_EXC_MAP) == 0:
            HTTPCORE_EXC_MAP = _load_httpcore_exceptions()
        try:
>           yield

<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:101: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:127: in __iter__
    for part in self._httpcore_stream:
<venv>/lib/python3.11/site-packages/httpcore/_sync/connection_pool.py:407: in __iter__
    raise exc from None
<venv>/lib/python3.11/site-packages/httpcore/_sync/connection_pool.py:403: in __iter__
    for part in self._stream:
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:342: in __iter__
    raise exc
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:334: in __iter__
    for chunk in self._connection._receive_response_body(**kwargs):
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:203: in _receive_response_body
    event = self._receive_event(timeout=timeout)
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:217: in _receive_event
    data = self._network_stream.read(
<venv>/lib/python3.11/site-packages/httpcore/_backends/sync.py:126: in read
    with map_exceptions(exc_map):
<stdlib>/lib/python3.11/contextlib.py:158: in __exit__
    self.gen.throw(typ, value, traceback)
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

map = {<class 'TimeoutError'>: <class 'httpcore.ReadTimeout'>, <class 'OSError'>: <class 'httpcore.ReadError'>}

    @contextlib.contextmanager
    def map_exceptions(map: ExceptionMapping) -> typing.Iterator[None]:
        try:
            yield
        except Exception as exc:  # noqa: PIE786
            for from_exc, to_exc in map.items():
                if isinstance(exc, from_exc):
>                   raise to_exc(exc) from exc
E                   httpcore.ReadTimeout: timed out

<venv>/lib/python3.11/site-packages/httpcore/_exceptions.py:14: ReadTimeout

The above exception was the direct cause of the following exception:

process_setup = ({'DACLAW_HOME': '<tmp>/pytest-0/test_api_and_worker_share_task0/daclaw_test', 'DARAOS_HARNESS_ARTIFACT_R..._task0/material/brief.md'], ...}, PosixPath('<tmp>/pytest-0/test_api_and_worker_share_task0/script.json'))
tmp_path = PosixPath('<tmp>/pytest-0/test_api_and_worker_share_task0')
lane = 'foreground'
record_property = <function record_property.<locals>.append_property at 0x7b91744ca8e0>

    @pytest.mark.parametrize("lane", ["foreground", "background"])
    def test_api_and_worker_share_tasks_across_processes(process_setup, tmp_path, lane, record_property):
        env, body, script = process_setup
        body["prefer_background"] = lane == "background"
        headers = {**AUTH_A, "Idempotency-Key": "cross-process-draft"}
        with api_server(env, tmp_path / "api-cwd") as api:
            assert api.post("/tasks", json=body).status_code == 401
            response = api.post("/tasks", json=body, headers=headers)
            assert response.status_code == 202, response.text
            task_id = response.json()["task_id"]
            assert api.get(f"/tasks/{task_id}", headers=AUTH_A).json()["status"] == "queued"
    
            with api.stream("GET", f"/tasks/{task_id}/events", headers=AUTH_A) as stream:
                assert stream.status_code == 200
                with child(
                    "harness.worker", ["--once", "--scripted", str(script), "--lanes", lane],
                    env, tmp_path / "worker-cwd",
                ) as worker:
>                   stream.read()

tests/harness_runtime/integration/test_processes.py:180: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
<venv>/lib/python3.11/site-packages/httpx/_models.py:881: in read
    self._content = b"".join(self.iter_bytes())
                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^
<venv>/lib/python3.11/site-packages/httpx/_models.py:897: in iter_bytes
    for raw_bytes in self.iter_raw():
<venv>/lib/python3.11/site-packages/httpx/_models.py:951: in iter_raw
    for raw_stream_bytes in self.stream:
<venv>/lib/python3.11/site-packages/httpx/_client.py:153: in __iter__
    for chunk in self._stream:
<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:126: in __iter__
    with map_httpcore_exceptions():
<stdlib>/lib/python3.11/contextlib.py:158: in __exit__
    self.gen.throw(typ, value, traceback)
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

    @contextlib.contextmanager
    def map_httpcore_exceptions() -> typing.Iterator[None]:
        global HTTPCORE_EXC_MAP
        if len(HTTPCORE_EXC_MAP) == 0:
            HTTPCORE_EXC_MAP = _load_httpcore_exceptions()
        try:
            yield
        except Exception as exc:
            mapped_exc = None
    
            for from_exc, to_exc in HTTPCORE_EXC_MAP.items():
                if not isinstance(exc, from_exc):
                    continue
                # We want to map to the most specific exception we can find.
                # Eg if `exc` is an `httpcore.ReadTimeout`, we want to map to
                # `httpx.ReadTimeout`, not just `httpx.TimeoutException`.
                if mapped_exc is None or issubclass(to_exc, mapped_exc):
                    mapped_exc = to_exc
    
            if mapped_exc is None:  # pragma: no cover
                raise
    
            message = str(exc)
>           raise mapped_exc(message) from exc
E           httpx.ReadTimeout: timed out

<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:118: ReadTimeout
_________ test_api_and_worker_share_tasks_across_processes[background] _________

    @contextlib.contextmanager
    def map_httpcore_exceptions() -> typing.Iterator[None]:
        global HTTPCORE_EXC_MAP
        if len(HTTPCORE_EXC_MAP) == 0:
            HTTPCORE_EXC_MAP = _load_httpcore_exceptions()
        try:
>           yield

<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:101: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:127: in __iter__
    for part in self._httpcore_stream:
<venv>/lib/python3.11/site-packages/httpcore/_sync/connection_pool.py:407: in __iter__
    raise exc from None
<venv>/lib/python3.11/site-packages/httpcore/_sync/connection_pool.py:403: in __iter__
    for part in self._stream:
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:342: in __iter__
    raise exc
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:334: in __iter__
    for chunk in self._connection._receive_response_body(**kwargs):
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:203: in _receive_response_body
    event = self._receive_event(timeout=timeout)
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
<venv>/lib/python3.11/site-packages/httpcore/_sync/http11.py:217: in _receive_event
    data = self._network_stream.read(
<venv>/lib/python3.11/site-packages/httpcore/_backends/sync.py:126: in read
    with map_exceptions(exc_map):
<stdlib>/lib/python3.11/contextlib.py:158: in __exit__
    self.gen.throw(typ, value, traceback)
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

map = {<class 'TimeoutError'>: <class 'httpcore.ReadTimeout'>, <class 'OSError'>: <class 'httpcore.ReadError'>}

    @contextlib.contextmanager
    def map_exceptions(map: ExceptionMapping) -> typing.Iterator[None]:
        try:
            yield
        except Exception as exc:  # noqa: PIE786
            for from_exc, to_exc in map.items():
                if isinstance(exc, from_exc):
>                   raise to_exc(exc) from exc
E                   httpcore.ReadTimeout: timed out

<venv>/lib/python3.11/site-packages/httpcore/_exceptions.py:14: ReadTimeout

The above exception was the direct cause of the following exception:

process_setup = ({'DACLAW_HOME': '<tmp>/pytest-0/test_api_and_worker_share_task1/daclaw_test', 'DARAOS_HARNESS_ARTIFACT_R..._task1/material/brief.md'], ...}, PosixPath('<tmp>/pytest-0/test_api_and_worker_share_task1/script.json'))
tmp_path = PosixPath('<tmp>/pytest-0/test_api_and_worker_share_task1')
lane = 'background'
record_property = <function record_property.<locals>.append_property at 0x7b91750c54e0>

    @pytest.mark.parametrize("lane", ["foreground", "background"])
    def test_api_and_worker_share_tasks_across_processes(process_setup, tmp_path, lane, record_property):
        env, body, script = process_setup
        body["prefer_background"] = lane == "background"
        headers = {**AUTH_A, "Idempotency-Key": "cross-process-draft"}
        with api_server(env, tmp_path / "api-cwd") as api:
            assert api.post("/tasks", json=body).status_code == 401
            response = api.post("/tasks", json=body, headers=headers)
            assert response.status_code == 202, response.text
            task_id = response.json()["task_id"]
            assert api.get(f"/tasks/{task_id}", headers=AUTH_A).json()["status"] == "queued"
    
            with api.stream("GET", f"/tasks/{task_id}/events", headers=AUTH_A) as stream:
                assert stream.status_code == 200
                with child(
                    "harness.worker", ["--once", "--scripted", str(script), "--lanes", lane],
                    env, tmp_path / "worker-cwd",
                ) as worker:
>                   stream.read()

tests/harness_runtime/integration/test_processes.py:180: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
<venv>/lib/python3.11/site-packages/httpx/_models.py:881: in read
    self._content = b"".join(self.iter_bytes())
                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^
<venv>/lib/python3.11/site-packages/httpx/_models.py:897: in iter_bytes
    for raw_bytes in self.iter_raw():
<venv>/lib/python3.11/site-packages/httpx/_models.py:951: in iter_raw
    for raw_stream_bytes in self.stream:
<venv>/lib/python3.11/site-packages/httpx/_client.py:153: in __iter__
    for chunk in self._stream:
<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:126: in __iter__
    with map_httpcore_exceptions():
<stdlib>/lib/python3.11/contextlib.py:158: in __exit__
    self.gen.throw(typ, value, traceback)
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

    @contextlib.contextmanager
    def map_httpcore_exceptions() -> typing.Iterator[None]:
        global HTTPCORE_EXC_MAP
        if len(HTTPCORE_EXC_MAP) == 0:
            HTTPCORE_EXC_MAP = _load_httpcore_exceptions()
        try:
            yield
        except Exception as exc:
            mapped_exc = None
    
            for from_exc, to_exc in HTTPCORE_EXC_MAP.items():
                if not isinstance(exc, from_exc):
                    continue
                # We want to map to the most specific exception we can find.
                # Eg if `exc` is an `httpcore.ReadTimeout`, we want to map to
                # `httpx.ReadTimeout`, not just `httpx.TimeoutException`.
                if mapped_exc is None or issubclass(to_exc, mapped_exc):
                    mapped_exc = to_exc
    
            if mapped_exc is None:  # pragma: no cover
                raise
    
            message = str(exc)
>           raise mapped_exc(message) from exc
E           httpx.ReadTimeout: timed out

<venv>/lib/python3.11/site-packages/httpx/_transports/default.py:118: ReadTimeout
=========================== short test summary info ============================
FAILED tests/harness_runtime/integration/test_processes.py::test_api_and_worker_share_tasks_across_processes[foreground]
FAILED tests/harness_runtime/integration/test_processes.py::test_api_and_worker_share_tasks_across_processes[background]
2 failed, 476 passed in 157.24s (0:02:37)

```
