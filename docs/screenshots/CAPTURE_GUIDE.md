# Screenshot capture guide

The README's **Screenshots** grid expects four PNGs in this folder. Capture them
once, drop them here with the exact filenames, and commit — the grid renders on
GitHub automatically.

## Setup (2 terminals)

```bash
uvicorn app.main:app --port 8000
```
```bash
API_BASE_URL=http://localhost:8000 streamlit run ui/app.py
```

Open http://localhost:8501. For a clean, consistent look: browser at **~1280×800**,
Streamlit **dark** theme (already the default via `.streamlit/config.toml`), and
hide the Streamlit menu if you like (Settings → *Wide mode* is already on).

## The four shots

| File | What to capture | How |
|---|---|---|
| `intake.png` | The **New investigation** form — identity, employment & wealth, account & onboarding, ID document, and the transaction-ledger editor. | On load, click **↻ Populate sample ledger**, then screenshot the form + ledger. |
| `results.png` | The **results** view — decision banner, the A2A flow row (with per-hop latency), the risk gauge + **radar**, and the score breakdown. | Prefill the **Alexei Volkov** sample → **Run detection** → screenshot the top of the results. |
| `findings.png` | The **Findings** tabs — open the **💸 AML transactions** tab to show the flagged-transactions table (and/or the **🎣 Fraud** tab). | Scroll to Findings, click the AML tab, screenshot. |
| `human-review.png` | The **⏸ Awaiting your review** panel — the human-in-the-loop gate with Approve / Override / Close. | Run a **Viktor Petrov** case (sanctions hit → pauses) and screenshot the review panel. |

Tips:
- PNG, roughly 1200–1600 px wide keeps the README grid crisp without bloating the repo.
- Keep each file under ~500 KB (crop to the relevant area).
- If you rename a file, update the matching `src="docs/screenshots/…"` in `README.md`.
