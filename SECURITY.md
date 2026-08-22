# Keys

Nothing in this repo reads a key from source. Both keys come from the
environment:

- `EODHD_API_KEY`      - market data
- `ANTHROPIC_API_KEY`  - the model stage (`--llm` only)

In GitHub, add them under **Settings > Secrets and variables > Actions**.
Never commit them, and never paste them into an issue or a chat window.
