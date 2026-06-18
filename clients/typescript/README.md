# @leptin/client

TypeScript client for [Leptin](https://github.com/lionellau/leptin) — the
satiety hormone for agent memory.

It wraps the Leptin local HTTP API (the same one the dashboard uses), so JS /
edge / browser code can read the savings ledger, browse memories, and trigger
guardrailed compaction.

```bash
npm install @leptin/client
```

Start the server:

```bash
leptin dashboard --port 8765
```

Use it:

```ts
import { LeptinClient } from "@leptin/client";

const mem = new LeptinClient("http://127.0.0.1:8765");

const report = await mem.report("all");
console.log(`Saved ${report.tokens_saved} tokens ($${report.usd_saved}).`);

const memories = await mem.memories("active");
const result = await mem.compact(/* dryRun */ true);
if (result.guardrail.rolled_back) {
  console.log("Compaction would hurt recall — auto-rolled-back.");
}
```

> Note: `remember` / `recall` are exposed to agents over **MCP** (stdio), not
> over this HTTP client. This SDK targets the read/operate surface used by
> dashboards and tooling.

MIT licensed.
