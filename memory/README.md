# memory

Keeper's long-term memory: one markdown file per topic, under `KEEPER_DATA_DIR/memory/`. In the sandbox that's `/sandbox/keeper/data/memory/`, on the sandbox's persistent volume.

The user-maintained `profile.md` in that directory is the opportunity radar's structured input. Its fields and missing-value rules are in [the profile contract](../docs/profile.md); [the demo profile](../demo/profile.md) is synthetic. Edit a real profile directly on the private data volume, not with `keeper memory add`, which appends dated topic notes.

```
# Deadlines

- 2026-10-20 14:03 UTC | NVIDIA GTC student hackathon closes Nov 1
```

```bash
keeper memory topics
keeper memory show deadlines
keeper memory add deadlines "GTC hackathon closes Nov 1"   # skips exact duplicates
keeper memory search hackathon
```

Code can use it directly. The opportunity radar does this to skip results it has already sent you:

```python
from memory import default_store
store = default_store()
if store.add("opportunities-seen", url):   # False if it's already there
    ...
```

Hermes keeps its own built-in memory too (MEMORY.md and USER.md, about 3,500 characters together). That's for the handful of facts it needs in every conversation. Everything else goes here, and the `keeper-memory` skill tells Hermes which is which.
