# Repository Guidelines

## Wider context: the NavAI estate

This repository is one of six. Cross-repo context — what the other services are,
which handle personal data, who owns what, and what has already been decided —
lives in the shared context repo:

**https://github.com/NavAI-pro/navai-context**

Read its `inventory.yaml` first if your task touches anything beyond this repo.
Its `AGENTS.md` carries the rules that apply everywhere: tenant isolation, no
personal data in logs, and how to record what you learn so the next person does
not rediscover it.

## Repo-specific guidance

The detailed guidance for this repository lives in [`CLAUDE.md`](CLAUDE.md) —
structure, commands, conventions and the things that will bite you. Read it.

It is kept in one file so the two agent toolchains this team uses cannot end up
following different rules.
