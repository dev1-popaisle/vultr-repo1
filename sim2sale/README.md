# Sim2Sale

Minimal buying-intent simulator dashboard + orchestration API on Cloudflare Workers,
powered by Vultr Serverless Inference. Additive to this repo — nothing else is touched.

## Test it

Live: **https://sim2sale.hidden-pond-5e6a.workers.dev**

1. Open the URL, click the dot in the header, paste a Vultr Inference API key.
2. Type the main question (full width), describe the product or upload a
   `.txt` / `.md` / `.json` / `.csv` file, set the audience.
3. Optional: Advanced → tech limits + depth (Quick / Standard / Deep).
4. Run. Results: Winner, Ranking, Segments heat grid, Fixes.

## Run locally

```bash
cd sim2sale
npm install -g wrangler
wrangler dev
```

## Deploy

```bash
cd sim2sale
wrangler deploy
# optional: bake the key in so the UI field isn't needed
wrangler secret put VULTR_INFERENCE_API_KEY
```

## API (same origin)

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET | `/api/health` | `{ok, name}` |
| GET | `/api/models` | live Vultr model list, needs key via `x-vultr-key` header |
| POST | `/api/variations` | `{product, business_question, tech_constraints, n}` → feasible variations |
| POST | `/api/personas` | `{product, business_question, target_audience, n}` → buyer personas |
| POST | `/api/trials` | `{product, variations, personas, runs}` → ranking + summaries + heat data |

Key resolution per request: `x-vultr-key` header → `apiKey` body field → `VULTR_INFERENCE_API_KEY` secret.

Worker cap: 48 trials per `/api/trials` call (request time budget). Larger sweeps
belong on the local FastAPI backend jobs endpoint; the dashboard defaults
(Quick 6 / Standard 12 / Deep 25 trials) stay inside the cap.
