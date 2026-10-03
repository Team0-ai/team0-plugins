# Team0 plugins

This repository contains the public plugin packages for [Team0](https://team0.ai).
The Team0 application and its hosted MCP service are maintained separately.

## Agent runtime: Claude Code, Codex, and OpenClaw

[`team0-agent-runtime/`](team0-agent-runtime/) is one shared package with native
adapters for all three hosts. Its hooks load relevant Living Understanding before
an ordinary turn and return completed turns for governed learning. Each host
needs its own Team0 connection and host-level plugin approval.

**Claude Code**

```sh
claude plugin marketplace add Team0-ai/team0-plugins
claude plugin install team0-agent-runtime@team0
```

**Codex**

```sh
codex plugin marketplace add Team0-ai/team0-plugins
codex plugin add team0-agent-runtime@team0
```

**OpenClaw**

In OpenClaw's **Plugins**, search for **Team0 Living Understanding** and install it on the
Gateway you use. [View the published plugin on ClawHub](https://clawhub.ai/team0-ai/plugins/team0-agent-runtime).
For CLI installation in that same Gateway environment:

```sh
openclaw plugins install clawhub:team0-agent-runtime
```

OpenClaw also needs a Team0 MCP connection and conversation-access approval in
its Gateway. See the [runtime setup and host-specific instructions](team0-agent-runtime/README.md)
before enabling its hooks. None of these installation commands grants trust or
connection consent automatically.

**Hermes**

In the active Hermes profile, open **Keys → Custom Keys** and save your Team0
access key as `TEAM0_HERMES_API_KEY`. Then open **Plugins → Install from GitHub / Git URL**,
enter `Team0-ai/team0-plugins/team0-agent-runtime`, select **Enable after install**,
and install. Review Hermes's security prompt yourself. Start a new chat after
activating the connection in Team0. The plugin runs before/after-turn hooks in
Hermes; no MCP connection or SOUL.md instructions are required for these hooks.

See the [Hermes setup](team0-agent-runtime/README.md#hermes) for the terminal alternative.

## ChatGPT

[`team0-chatgpt/`](team0-chatgpt/) is the source package for Team0's ChatGPT
plugin. It connects to Team0's hosted MCP service for on-demand reads and
contributions. Unlike the native agent runtime, ChatGPT does not run Team0
before- and after-turn hooks automatically. This GitHub folder is source code,
not a ChatGPT Store listing or a Claude/Codex marketplace entry; installation
and OAuth consent happen in ChatGPT.

Claude.ai, Claude Cowork, and Town connect to the hosted Team0 MCP service;
they do not have downloadable packages in this repository.

You can stop an agent's access or contributions in Team0 under **Agents**.
For help, contact support@team0.ai.
