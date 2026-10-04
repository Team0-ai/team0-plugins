# Security

Please report a suspected vulnerability in this plugin or in the Team0 service privately to
**hey@team0.ai** (subject: Security), not in a public issue. Include the plugin version, the host (Claude Code,
Codex, OpenClaw or Hermes) and the steps to reproduce.

What the plugin handles:

- Your Team0 access key, kept in the host's own configuration or credential store and never placed
  in a prompt.
- Before each turn, a read of the Team0 understanding the key is allowed to see.
- After each turn, your message and the agent's final reply, sent to Team0 over HTTPS.

You can stop an agent from saving, or revoke its access, at any time from Team0's Agents page.
