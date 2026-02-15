# Contributing to memU

Thanks for wanting to help make AI memory free for everyone.

## How to contribute

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes
4. Run tests: `pytest`
5. Run linting: `ruff check .`
6. Open a PR

## What we need help with

- **Integrations** — LangChain, LlamaIndex, CrewAI, AutoGen adapters
- **Embedding providers** — support for local models (Ollama, sentence-transformers)
- **Dashboard** — web UI for browsing/searching memories
- **Memory compression** — summarizing old memories to save space
- **Tests** — more coverage is always welcome

## Guidelines

- Keep it simple. memU's strength is being easy to run.
- Type hints on all public functions.
- Docstrings on all public classes and methods.
- If adding a dependency, justify it in the PR.

## Questions?

Open an issue or reach out at hello@protelynx.ai.
