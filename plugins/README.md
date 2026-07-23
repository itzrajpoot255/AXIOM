# Plugins

Any `*.py` file in this directory is loaded at startup. A plugin adds
tools by exporting a `register(registry, ctx)` function:

```python
# plugins/dice.py
import random

def register(registry, ctx):
    @registry.register("roll_dice", "Roll an N-sided die",
                       {"?sides": "integer: default 6"})
    def roll_dice(ctx, sides: int = 6):
        return f"Rolled a {random.randint(1, int(sides))} (d{sides})."
```

The `ctx` object gives access to `ctx.cfg` (config), `ctx.memory`
(long-term memory), `ctx.models` (model manager), `ctx.scheduler`,
and `ctx.notify(text)` for proactive messages.

Files starting with `_` are ignored.
