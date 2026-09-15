# ChatGPT Work Instructions

Development source for Momentum Scanner, Orderflow, Papertrading and 10%-Shadow.

- Local runtime: C:\TradingSuite\MomentumScanner
- Databases: C:\USMomentumData
- Never commit .env, credentials, databases or generated trading data.
- GitHub is the development/version-control source.
- Actual execution remains local on Windows.
- Prefer small, reviewable changes.
- Do not fabricate candidate CSV files or trading data.
- Do not rewrite live SQLite databases.
- Inspect existing code before changing behavior.

Validation:
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
