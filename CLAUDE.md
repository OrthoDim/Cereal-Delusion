# AI Science Hack 2026

## Reference Repos

Two reference repositories are cloned locally (gitignored) for context:

### Elnora CLI (`./elnora-cli/`)
- Elnora AI platform CLI for bioprotocol optimization
- Installed as a Claude Code plugin via `pluginDirs` in `.claude/settings.json`
- Skills are in `elnora-cli/skills/` — Claude Code loads these automatically
- Source code in `elnora-cli/src/elnora/`

### PyLabRobot (`./pylabrobot/`)
- Open-source Python framework for controlling liquid-handling robots and lab automation hardware
- Docs source in `pylabrobot/docs/`
- Library source in `pylabrobot/pylabrobot/`
- When writing code that interfaces with lab hardware, follow PyLabRobot conventions and API patterns

## Image Fetching

- `python data_pipeline/fetch_images.py` — fetches all Cereal Delusion plate images from Monomer Cloud MCP into `monomer_images/` (requires MCP auth via `~/.claude/.credentials.json`)

## Conventions

- Follow PyLabRobot API patterns when writing lab automation code
- Use the Elnora CLI (via plugin skills) for interacting with the Elnora platform
- Python 3.10+
