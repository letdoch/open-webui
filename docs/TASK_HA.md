# Task drain and instance liveness (design, revision 2)

Status: **design for review round 2**. Rev 1 was reviewed by two reviewers (not ready, 9 major findings); rev 2 adopts
the shared answers and the orchestrator's ruling that the reaper is required (3.4). Scope: the fork of Open WebUI v0.13.3
(`backend/open_webui`). Two work packages of a high-availability programme for deployments that run several Open WebUI
instances on one shared Valkey and one shared database:

- **WP-O1 drain hook**: a stopping instance stops accepting chats, lets its running chats finish up to a bound, and ends
  the rest with a terminal error event and clean task keys.
- **WP-O2 instance liveness**: a killed instance's chats end with an error in the clients and their task keys are
  cleared by its peers within seconds instead of after `REDIS_TASK_TTL` (300 s).

Constraint (fork rule, `docs/UPSTREAM_INTEGRATION.md`): the fork stays rebaseable on upstream. All new logic lives in
`backend/open_webui/gateway_bridge/task_ha/`; upstream files get one-line hooks carrying the `# gateway-bridge` marker.
Configuration is declarative (the bridge settings file), and a disabled block leaves every code path stock.

## 1. Facts of the current code (v0.13.3 fork, evidence by file)

| Id | Fact | Consequence |
| --- | --- | --- |
| F-1 | A chat is an in-process `asyncio.Task` created by `create_task` (`tasks.py:160-181`, call site `main.py:1914`). The HTTP request returns at once with `task_ids`; the task lives in the module dict `tasks`, and `item_tasks` maps a chat id to its task ids. The fan-out stores `meta.task_id` on the assistant message (`main.py:~1878`). | uvicorn's graceful stop waits for open HTTP requests, not for these tasks. |
| F-2 | The lifespan (`main.py:370-545`) cancels listener/heartbeat tasks after `yield` and does nothing about `tasks`. | At shutdown running chats are killed with the event loop; no terminal event is sent for them. |
| F-3 | Registration in Valkey (`redis_save_task`, `tasks.py:90-97`): string `{P}:tasks:{task_id}` = `'1'` with `EX REDIS_TASK_TTL`, hash `{P}:tasks` (`task_id -> item_id`), set `{P}:tasks:item:{item_id}`. `P` is `REDIS_KEY_PREFIX`. | Active state is the existence of the string key. |
| F-4 | `redis_task_heartbeat` (`tasks.py:73-85`) runs `EXPIRE` on the keys of the local tasks every `REDIS_TASK_TTL/4` (75 s). | A live instance keeps its keys; a dead one stops refreshing them. |
| F-5 | `redis_list_tasks` (`tasks.py:110-133`) returns the ids whose string key exists and deletes the hash and set entries of the others (lazy clear on read). Every decision point reads through it (`has_active_tasks`, chat list, `/api/tasks/chat/{id}`, timers, subagents, compact/fork guards). | Expiry is evaluated by readers. |
| F-6 | After a hard kill the string keys expire 225-300 s later. | The stuck interval is the TTL, not a property of the instance. |
| F-7 | When `error_detail` is set, the inner `finally` of `process_chat` (`main.py:1750-1783`) persists `error`/`done` on the message and emits `chat:message:error`; a cancelled task already emits `chat:tasks:cancel` and finalizes the message (`utils/middleware.py:6559-6602`, `main.py:1723-1745`). | A drain only has to set `error_detail` on the cancel path. |
| F-8 | `/ready` (`main.py:3163`) reports startup, database and Valkey. | The natural place to say "draining". |
| F-9 | Websocket events go through `WEBSOCKET_MANAGER=redis`; a client of a stopped instance reconnects to another instance (about 1-2 s) and receives events published from any instance. | A draining or reaping instance can still tell the client its chat ended. |
| F-10 | After a reconnect the client reads the running tasks of the open chat once (`Chat.svelte:1972-1979`); later changes reach it only as events. | A silent clear leaves a tab spinning: peers must emit the end of the chat. |
| F-11 | The scheduler loop (`utils/automations.py:69-126`) claims due timers and automations and runs them through the chat handler with synthetic requests (`/api/v1/{automations,timers,subagents}/internal`, no matched route). | Work claimed by a draining instance must not be refused; draining instances must stop claiming. |
| F-12 | `start.sh` runs uvicorn without `--timeout-graceful-shutdown`; uvicorn then waits without limit for open HTTP requests before the lifespan shutdown. `REDIS_SOCKET_TIMEOUT` has no default (`env.py:406-410`). | The stop bound and Valkey socket bounds are a platform contract (3.7). |

## 2. Requirements

| Id | Requirement |
| --- | --- |
| Q-1 | On `draining`: refuse new chats from external HTTP clients with a typed 503, report `/ready` false, stop claiming scheduled work; running work and server-started work continue. |
| Q-2 | On stop: wait for the local `tasks` dict until `max_stream_seconds` after the stop signal (default 300 s), independent of uvicorn's own wait. |
| Q-3 | At the bound: cancel the remainder; each chat ends with a persisted and emitted error `server_draining`, and its Valkey keys are removed. |
| Q-4 | A hard kill: within `instance_ttl_seconds` (30 s) plus two reaper periods (10 s) of the last heartbeat, a peer ends every task of the dead instance that can be addressed: error on the message, `chat:message:error` and `chat:active=false` (anchored to a message of the chat) to the owner's clients, keys cleared. Saved chats always; temporary chats when the task has a stream snapshot. |
| Q-5 | A disabled block leaves every path stock; mixed versions during a rolling update stay correct. |
| Q-6 | Reads add at most one Valkey pipeline (the owners' liveness) when foreign owners are present; with N behind a balancer that is about (N-1)/N of the reads. |
| Q-7 | A live instance's tasks are not ended by a peer across a bounded Valkey failover, a late beat or a lost registration; an owner stalled longer than the TTL plus one sweep (about 35 s) is treated as dead, and when it resumes it cancels the tasks that were reaped meanwhile. |

## 3. Design

### 3.1 States of an instance

`serving` -> `draining` -> `quiescing` -> `stopped`.

- `draining` starts on `SIGTERM`, or when the configured **drain file** exists (a supervisor's early drain request: it
  takes the instance out of the balancer first, then stops it). It sets a process flag read by `/ready` (503), by chat
  admission (503, `Retry-After: 1`, external HTTP requests only) and by the scheduler loop (no further claims).
- `quiescing` starts at `SIGTERM`: the signal handler records `terminated_at` and schedules the drain coroutine on the
  loop; uvicorn's own shutdown proceeds independently and the lifespan shutdown only awaits that coroutine (3.5).
- `stopped`: the instance key (3.2) is deleted; the process exits.

`SIGTERM` chains to uvicorn's own handler (the previous handler is called). `SIGTERM` alone is always sufficient; the
drain file is the optional early step, polled every second by a watcher task (works with any number of worker processes,
under compose and under systemd, no signal that uvicorn's multi-worker supervisor would turn into a hard stop).

### 3.2 Data model (Valkey, prefix `P`)

Existing keys keep their name and type (F-3). Changes, all backward compatible:

| Key | Type | Value | TTL | Writer | Readers |
| --- | --- | --- | --- | --- | --- |
| `{P}:tasks:{task_id}` | string | **owner instance id**; legacy value `1` = owner unknown | `REDIS_TASK_TTL`, refreshed by the stock heartbeat (F-4), unchanged | `redis_save_task` of the owner; the heartbeat (`SET NX`) when missing | every instance |
| `{P}:tasks:instance:{instance_id}` | string | `1` | `instance_ttl_seconds` (30), refreshed every `instance_heartbeat_seconds` (5) | the owning process | every instance (existence) |
| `{P}:tasks:reap:{task_id}` | string | `1` | 60 s | the reaper, `SET NX` | the reaper (claim) |

`instance_id` is a random UUID (hex) drawn once at lifespan startup (after any fork), so each uvicorn worker process is
its own instance and a restarted process is a new instance. Compatibility: old readers only test existence of the task
key and ignore the value; new readers treat `1` as "owner unknown, rely on the TTL", never as dead. A disabled instance
writes `1`. No key is renamed or deleted by the change.

**Time.** The only clock that decides liveness is Valkey's key expiry. Valkey 7 and later replicate expiries as absolute
times, so after a failover the promoted node's clock applies: Valkey nodes must be NTP-synchronised (deployment
requirement). Application-host clocks are not compared anywhere.

**Settings rule.** `instance_ttl_seconds - instance_heartbeat_seconds >= valkey_failover_window_seconds + 10`
(defaults 30 - 5 >= 15 + 10), enforced by the settings validator: at the loss of a primary the instance key still has at
least the failover window left, so a bounded failover never declares a live instance dead.

### 3.3 Liveness (WP-O2)

One asyncio task per process, every `instance_heartbeat_seconds`, **each run bounded by `asyncio.wait_for` of one beat**:

```
SET {P}:tasks:instance:{id} 1 EX instance_ttl_seconds
for every local task that is not done and not cancelling():           # pipeline
    SET {P}:tasks:{tid} <id> EX REDIS_TASK_TTL NX ; HSET {P}:tasks tid item ; SADD {P}:tasks:item:{item} tid
for every such task no longer in the local dict after the pipeline:   # finished meanwhile
    redis_cleanup_task
```

The write of the instance key also happens once in the lifespan startup (bounded, failure only logged), before the application reports ready. Re-registering
**every beat** (not on an inferred "key was missing" event) restores a registration lost to async failover or cleared
by a peer, whatever the cause; `NX` leaves existing keys and their TTL alone, so the cost is one pipeline of three
commands per local task per beat. A beat that hangs is abandoned after one period and retried.

Reader (`redis_list_tasks` delegates to `TaskHa.list_tasks` when enabled; the stock body is untouched):

```
owners = GET {P}:tasks:{tid}  for every listed id
dead   = owners that are neither own id, "1" nor missing and whose {P}:tasks:instance:{owner} does not exist   # one pipeline
owner missing  -> cleared as stock (expired)
owner in dead  -> hidden from the result, left for the reaper
else           -> active
```

### 3.4 Reaper (WP-O2, ends the dead instance's chats)

Every instance runs the reaper loop every `instance_heartbeat_seconds` (bounded by the instance TTL):

```
items  = HGETALL {P}:tasks ; owners = GET each ; dead = dead owners
confirmed = dead observed in this sweep AND the previous sweep        # a late beat re-registers within one period
for each task of a confirmed owner:
    SET {P}:tasks:reap:{tid} 1 NX EX 60  -> only the winner continues
    message = the assistant message of the chat with meta.task_id == tid and done == false
    persist  error {"content": "server_unavailable: ..."}, done = true      (the existing upsert used by the error path)
    emit chat:message:error {error, done:true} to the chat owner's room    (get_event_emitter, update_db=False)
    redis_cleanup_task(tid)                                                   (always, even if finalizing failed)
    if the chat has no active task left: emit chat:active {active:false} anchored to a message of the chat (see below)
```

This is what ends a spinning tab (F-10): the client is reconnected to a live instance within about 2 s, well before the
reap (at least 30 s after the kill), and receives the events. Reaping by several instances is serialised by the claim;
if the winner dies the claim expires and the next sweep retries. The two-sweep rule plus the heartbeat's re-registration
(3.3) is what protects a live instance (Q-7): an owner that missed one beat is back before the second sweep.

Worst case after `kill -9`: last beat + 30 s (key expiry) + up to 2 sweep periods (10 s) = about 40 s, against 300 s.

The events are anchored to a real message of the chat, because the frontend ignores events of messages it does not hold
(`Chat.svelte`): the finalized message when one is found, otherwise (message already done) the chat's current message,
for `chat:active=false` only. Temporary chats have no stored message; they are ended through the stream snapshot (4).

### 3.5 Drain (WP-O1)

```
admit(request):     # first line of chat_completion; external HTTP request = matched route and not request.state.internal
    if draining and external: raise HTTPException(503, "Server is draining", Retry-After: 1)

SIGTERM handler:    terminated_at = monotonic; draining = true; loop.call_soon_threadsafe(start drain coroutine); call uvicorn's handler

drain coroutine:
    deadline = terminated_at + max_stream_seconds
    wait for the local task dict (asyncio.wait, re-snapshot) until empty or deadline
    for each still pending task: drain_cancelled.add(task); task.cancel()
    asyncio.wait(still pending, timeout = cancel_grace_seconds)
    for every task id seen: cleanup_task(redis, id, item)                  # idempotent; covers lost done-callbacks

lifespan shutdown (first statement after `yield`): await the drain coroutine (start it if no signal), stop the loops, DEL the instance key
```

The deadline counts from the signal, so uvicorn's wait for open HTTP requests cannot delay the cancel; the process
exit itself is bounded by the platform contract (3.7).

Terminal error: the cancel path of `process_chat` (F-7) sets `error_detail = TaskHa.cancel_error()` (one line). That is
non-null only for a task in the `drain_cancelled` owned task set (task identity, with done-callbacks removing finished
tasks; robust against lost `CancelledError` messages), so the stock `finally` persists `error: {content: "server_draining: ..."}` and emits `chat:message:error`
after `chat:tasks:cancel`, and a user's stop keeps the stock behaviour.

Admission refuses only external HTTP chat requests, in both branches (the check is the first statement of the handler).
Server-started work (scheduler, timers, automations, subagent continuations) builds a request without a matched route and
is never refused; the scheduler loop stops claiming when draining starts, so due work is claimed by peers.

### 3.6 Settings (declarative, bridge settings file)

A nested block `task_ha` of `GatewayBridgeSettings` (frozen Pydantic, `extra=forbid`), independent of the bridge's `enabled`.

| Field | Type / bound | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | everything below is inert when false |
| `max_stream_seconds` | int 1..86400 | 300 | drain wait from the stop signal |
| `cancel_grace_seconds` | int 1..300 | 30 | wait after cancel before forcing key cleanup (each cancelled chat does several database writes; sized by the load run) |
| `instance_heartbeat_seconds` | int 1..60 | 5 | instance key refresh, reaper period, bound of one beat |
| `instance_ttl_seconds` | int 1..600 | 30 | declares an instance dead (rule in 3.2) |
| `valkey_failover_window_seconds` | int 1..300 | 15 | the bounded failover the TTL must survive |
| `drain_file` | str | empty | path whose existence starts draining (empty: only `SIGTERM`) |
| `drain_settle_seconds` | int 0..60 | 5 | minimum time after draining began before the task dict may be judged empty |

### 3.7 Platform contract (the deployment's duty, not fork code)

Without these the fork cannot meet its bounds; they are work of the deployment package (WP-O3 and the platform), listed
here so the proof run sets them:

1. uvicorn `--timeout-graceful-shutdown <= max_stream_seconds` in the start script (explicit uvicorn arguments replace
   `start.sh`'s defaults, so `--workers` and `--ws-per-message-deflate` are repeated there).
2. `REDIS_SOCKET_TIMEOUT` and `REDIS_SOCKET_CONNECT_TIMEOUT` set, each not above the heartbeat period (no default exists).
3. Supervisor stop timeout (compose `stop_grace_period`, systemd `TimeoutStopSec=`) of at least
   `max_stream_seconds + cancel_grace_seconds` plus margin; under compose the default of 10 s turns every stop into a kill.
4. Valkey nodes NTP-synchronised; Valkey 7 or later.
5. A supervisor that wants readiness false before the stop creates the drain file (`docker exec ... touch`, a systemd
   `ExecStop` pre-step) and waits one probe interval before the signal. The drain file path comes from the settings file:
   instances that share a settings file share the file, so on one host each instance needs its own settings file or path.

### 3.8 Hook list (the whole patch to upstream files)

| File | Hook (each one line unless noted) |
| --- | --- |
| `tasks.py` | import; `redis_save_task`: value `TaskHa.owner_token()`; top of `redis_list_tasks`: `if TaskHa.enabled(): return await TaskHa.list_tasks(...)` (two lines) |
| `main.py` | imports (`TaskHa`, `OpenWebuiTaskStore`); lifespan startup `await TaskHa.start(app, OpenWebuiTaskStore)`; lifespan shutdown `await TaskHa.quiesce()`; `/ready`: 503 while draining (two lines); `TaskHa.admit(request)` first line of `chat_completion`; `error_detail = TaskHa.cancel_error()` before the re-raise of `except asyncio.CancelledError` |
| `utils/automations.py` | import; scheduler loop: skip claims while draining (three lines) |
| `gateway_bridge/bridge_settings.py` | the `task_ha` field |
| `docs/UPSTREAM_INTEGRATION.md` | hook table and module list |

Module layout: `gateway_bridge/task_ha_settings.py` (the `TaskHaSettings` model, a sibling of `bridge_settings.py` so that
the settings module has no import cycle), `task_ha/local_tasks.py` (`LocalTasks` protocol), `task_ha/orphan_store.py`
(`OrphanStore` protocol), `task_ha/task_store.py` (`TaskStore` combined storage protocol), `task_ha/orphan_target.py`
(`OrphanTarget` model), `task_ha/liveness.py` (identity, heartbeat key, dead-owner check, bounded periodic runner),
`task_ha/drain.py` (state, signal, drain coroutine, admission, cancel marker), `task_ha/reaper.py` (sweep, claim),
`task_ha/process_runtime.py` (`TaskHaRuntime` collaborator assembly), `task_ha/store.py` (`OpenWebuiTaskStore`, the only
code that touches Open WebUI's task, chat and event modules), and `task_ha/runtime.py` (the `TaskHa` facade used by the
hooks). Typed models, one top-level class per module, no `dict[str, Any]`, files far below 1000
lines.

## 4. Failure analysis

| Case | Behaviour |
| --- | --- |
| Instance `kill -9`, Valkey alive | Reaper of a peer ends its chats about 40 s after the last beat at worst: error on the message, events to the reconnected clients, keys cleared. |
| Valkey unreachable | Nothing to clear; each beat is abandoned after one period and retried; stock failure behaviour (new chats fail) unchanged. |
| Valkey failover within `valkey_failover_window_seconds` | The instance key survives on the promoted node (rule 3.2); no instance is declared dead. |
| Failover longer than the TTL, or a task registration lost | Keys may be gone; the next beat restores every running local task (`SET NX`); a reaper that saw the owner dead needs a second sweep to act, and by then the beat has restored the instance key. |
| Event loop stalled longer than the TTL | Same as above after the loop resumes. |
| `SIGTERM` while a chat is mid-stream | Admission and scheduler stop; the chat continues to its end or to the bound; keys removed at its end by the stock `cleanup_task`. |
| `SIGTERM` with an HTTP stream held open | uvicorn waits (bounded by `--timeout-graceful-shutdown`); the drain coroutine runs independently and cancels tasks at the bound. |
| Stream longer than the bound | Cancelled; message finalized with error `server_draining`; client sees the event through another instance (F-9); user resubmits. |
| Many cancels at the bound | Each does several database writes; `cancel_grace_seconds` (30) is sized by the load run; stragglers are logged, their keys are cleared, and the reaper never touches them (the instance is alive). |
| Supervisor kills before the bound | Falls back to the kill case. |
| Rolling update, old and new instances mixed | Old keys (`1`) expire by TTL only; new keys are valid for old readers. |
| Redis not configured | Drain works on the local dict; heartbeat, reaper and owner checks are skipped. |
| Automation or timer due during the drain | The draining instance does not claim; a peer claims it. A run claimed just before draining and not yet registered as a task is covered by the settle period: the drain does not judge the task dict empty before `drain_settle_seconds` (5 s) after draining began; already registered runs are not refused and are waited for. |
| Valkey unreachable at startup | The start beat is bounded and its failure only logged; the instance starts (stock startup sends no Valkey command) and the periodic beat retries. |
| Temporary chat of a dead instance | The reaper reads the task's response-stream snapshot (chat and message id) and the temporary chat's owner, and emits through the same emitter, which routes to the owner's session; without a snapshot (the chat produced no output yet) only the keys are cleared. |
| Owner stalled longer than TTL plus one sweep, then resumes | Its chat was ended by a peer; the next beat restores the key with `SET NX`, sees the reap claim and cancels the local task, so the stream does not continue into a message the user was told had ended. |

## 5. Proof (recorded dogfooding run, no unit tests)

Topology: two replicas of the fork image with the patched `backend/open_webui`, PostgreSQL as the database, the Valkey
Sentinel topology of the deployment (primary, replica, three Sentinels) with the Sentinel variables, an OpenAI-compatible
stub that streams for minutes. Clients: the HTTP API for load plus a socket.io client on the **peer** recording events
(no browser is available on the host; the socket client records the same events `Chat.svelte` consumes, and one
browser-driven pass is added if a browser can be reached). Platform contract (3.7) applied.

1. Control: patch disabled, 50 chats on A, `kill -9` A: the keys stay about 225-300 s (recorded once).
2. Kill: patched, 50 concurrent long chats on A (and 20 on B), `kill -9` A: time to the first `task_ids == []` on B, time to
   `chat:message:error` and `chat:active=false` on the peer's socket (the `message_id` of each event must be a message of that
   chat: the frontend ignores others), persisted error count equal to 50, B's own chats unaffected.
3. Drain graceful: 50 chats, drain file, `/ready` 503 and a new chat 503 and an automation/timer due in the window claimed by
   B; `systemctl --user stop` (native units, `TimeoutStopSec` per the contract; `docker stop` for a container run): chats
   shorter than the bound complete, the process exits.
4. Drain at the bound: `max_stream_seconds` shorter than the streams, 50 chats cancelled at once: count of persisted
   `server_draining` errors equals the cancelled count within the grace; peer socket received the events; no key left;
   exit time.
5. HTTP stream held across `SIGTERM` (direct completion without session id): uvicorn bound honoured, tasks cancelled at the bound.
6. Sentinel failover during running chats (`SENTINEL FAILOVER`, then a primary kill): chats stay listed and stoppable, no
   chat ended by a peer, no reaped task.
7. `SIGSTOP` of a live instance longer than the TTL, `SIGCONT`: its chats are restored or, if a peer reaped them
   (stopped longer than TTL plus two sweeps), the outcome is recorded.
8. Mixed version: a stock-image replica next to a patched one: each sees the other's tasks, no key errors.
9. The same stop/kill pair under a systemd unit where the host allows running the fork without Docker (otherwise left to
   the T1 assembly proof, stated in the record).

The run (commands, timestamps, observed intervals) is recorded in the cframe WORKPLAN row of the package.

## 6. Points for round 2

1. Reaper decision (required by the orchestrator): two-sweep confirmation, claim, finalize by `meta.task_id`.
2. Cost: heartbeat 1 SET plus one pipeline per local task per beat; reaper 1 HGETALL plus 2 pipelines per sweep for the
   whole cluster's tasks per instance (about 300 tasks, every 5 s per instance): acceptable, to be measured in the load run.
3. The `drain_file` and the platform contract of 3.7 as the interface to the deployment.

## 7. Proof record (2026-11-18, native run)

Topology: two replicas of the fork (image `cframe-openwebui:local` filesystem, Python 3.12, patched `backend/open_webui`) as
`systemd --user` units without Docker, PostgreSQL 17 as the database, Valkey primary + replica + three Sentinels (Sentinel
variables, `REDIS_SOCKET_TIMEOUT=4`), an OpenAI-compatible stub streaming 1 chunk/s (240 s per chat), `--timeout-graceful-shutdown`
set per run, a socket.io client on replica B recording the events of the user room. 50 concurrent chats on A (20 on B in the kill run).

| Step | Observation |
| --- | --- |
| 1 Control, stock code, `kill -9` A, 50 chats | task keys listed for **295.5 s**; the 50 messages stay unfinished (`done=0`, no error); no event |
| 2 Patched, `kill -9` A, 50 chats on A + 20 on B | listing hides A's tasks after **31 s**; the reaper of B ended all 50 at **39.8-40.6 s** (`Reaped 50 task(s)`): 50 persisted `server_unavailable` errors, 50 `chat:message:error` and 50 `chat:active` events on the peer socket, every event's `message_id` is a message of its chat; B's 20 chats untouched |
| 3 Graceful drain: drain file, `systemctl stop`, 40 s streams, bound 60 s | `/ready` 503 on A (200 on B), a new chat on A got `503 {"detail":"Server is draining"}`, an automation made due in the window was claimed by B (success 1) and by A 0 times; A exited **32.9 s** after SIGTERM when the streams ended; 50/50 chats done, 0 errors |
| 4 Many cancels at the bound (20 s), 50 chats | A exited **21.7 s** after SIGTERM; 50 persisted `server_draining` errors, 50 `chat:message:error` on the peer socket with valid message ids, no task key left |
| 5 Direct HTTP stream held open across SIGTERM (uvicorn graceful 25 s, bound 20 s) | A exited after **25.3 s** (uvicorn bound); the direct stream was cut; the 5 session chats were cancelled at the 20 s bound with `server_draining` (5/5), independently of uvicorn's wait |
| 6 Sentinel: `SENTINEL FAILOVER`, then a kill of the new master, 50 chats | graceful failover: 50 tasks listed throughout, 0 read errors; master kill: failover to the other node, 4 failed reads in the window, 50 tasks listed after, a chat stopped through the peer (49 left), 0 `server_unavailable`, no reap |
| 7 `SIGSTOP` of A, 20 chats: 20 s stall | no change while stopped, no reap, 20 tasks after resume (`done=0`, no errors) |
| 7 `SIGSTOP` of A, 55 s stall (40 chats) | hidden after 31 s, **reaped by B at 36 s** (40 tasks); on `SIGCONT` A logged "was ended by a peer ... cancelling" for the reaped tasks and kept none (the earlier batch was reaped by the same stall) |

Not run in this record: the mixed-version pair (step 8 above: one stock replica next to a patched one; the data model keeps the
stock value `1` and the rule "never dead" for it), 300 concurrent streams (50 used, the host was shared with performance
measurements), a browser-driven pass, and the stop/kill pair under the deployment's own units (this run used native units
of the rig). The Sentinel rig needed `announce-ip` per Sentinel on loopback (rig detail, not product behaviour).
