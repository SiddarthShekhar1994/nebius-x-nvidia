# router

`route(task_type, messages, tools=None, **params)` picks a Nemotron model for the job, calls Token Factory, logs the call to SQLite, and returns the normal OpenAI chat completion.

```python
from router import route

resp = route("triage", [{"role": "user", "content": "Bucket this email: ..."}])
print(resp.choices[0].message.content)
```

| Task type | Model | Model id |
| --- | --- | --- |
| `classify`, `triage`, `radar` | Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
| `tool_call`, `draft` | Super | `nvidia/nemotron-3-super-120b-a12b` |
| `plan`, `skill_write` | Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` |

Any other task type raises `UnknownTaskType` before a request goes out. Extra keyword arguments, such as `max_tokens`, `temperature` and `tool_choice`, are passed straight to `chat.completions.create`. You can override the model ids with `KEEPER_MODEL_NANO`, `KEEPER_MODEL_SUPER` and `KEEPER_MODEL_ULTRA`.

## Call log

Every call adds one row to `calls` in `KEEPER_DATA_DIR/router.db`, or in `KEEPER_ROUTER_DB` if that's set. Failed calls are logged too, and the error is raised again so the caller can see it.

| Column | Meaning |
| --- | --- |
| `ts` | UTC timestamp, ISO 8601 |
| `task_type` | What the caller asked for |
| `model` | Model the router requested |
| `served_model` | Model the API says answered. It differs from `model` if something in between swapped it. |
| `prompt_tokens`, `completion_tokens` | From the API's `usage` |
| `latency_ms` | One request. Retries are off, so this is never a total across retries. |
| `cost_usd` | Estimate from Token Factory list prices in `pricing.py`, based on the model that answered |
| `status`, `error` | `ok` or `error`, plus the error text |

If the log can't be written, the error is logged and the call still returns normally.

## Command line

Run these from the repo root.

```powershell
py -m router smoke     # one live call per task type, then shows the rows it logged
py -m router recent    # last 20 calls
py -m router summary   # calls, tokens, average latency and cost per model
py -m unittest discover tests   # offline tests, no key needed
```

`smoke` fails if any task gets no reply, if the tool call doesn't come back, or if the API reports a different model from the one requested.

## Running inside the sandbox

Inside the NemoClaw sandbox, the obvious route to Token Factory is `inference.local`. That's what Hermes uses. But OpenShell's docs say that route rewrites the `model` field to the one model the route is configured with, which for us is Super. So Nano and Ultra requests sent that way would all quietly become Super calls. If that happens, the log will show it in `served_model`.

OpenShell's supported way for a workload to choose its own model is a provider attachment: a provider profile for `api.tokenfactory.us-central1.nebius.com`. The router calls that host directly with a placeholder key, and OpenShell substitutes the real key at egress, so the key still never enters the sandbox. The router code is the same either way; only `TOKENFACTORY_BASE_URL` and the key it reads change. That setup belongs with the first skill that runs inside the sandbox, which is item 3.
