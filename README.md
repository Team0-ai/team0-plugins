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
claude plugin marketplace add ybentov1/team0-plugins
claude plugin install team0-agent-runtime@team0
```

**Codex**

```sh
codex plugin marketplace add ybentov1/team0-plugins
codex plugin add team0-agent-runtime@team0
```

**OpenClaw**

```sh
openclaw plugins install team0-agent-runtime --marketplace ybentov1/team0-plugins
```

OpenClaw also needs a Team0 MCP connection and conversation-access approval in
its Gateway. See the [runtime setup and host-specific instructions](team0-agent-runtime/README.md)
before enabling its hooks. None of these installation commands grants trust or
connection consent automatically.

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
