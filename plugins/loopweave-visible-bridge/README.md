# LoopWeave Visible Review Bridge

Local Codex Desktop idle observer for exactly-once delivery of a pending
LoopWeave review into one explicitly bound visible task.

The worker publishes a durable pending card and normally dispatches it through
Desktop's owner-routed local IPC. If the bound task is active, this plugin waits
without using a model, verifies the Desktop-owned task identity, and invokes the
same dispatcher after the task becomes idle. It never creates a hidden reviewer,
replacement worker, recurring automation, or standalone app server.

## Lifecycle

```bash
loopweave bridge install
loopweave bridge bind --thread <task-id> --run-id <run-id>
loopweave bridge doctor --json
loopweave bridge status --json
loopweave bridge unbind
loopweave bridge uninstall
```

Installing or updating the plugin requires a Codex Desktop restart. Closing
Desktop stops delivery naturally; pending reviews remain queued. Uninstalling
removes only the plugin and bridge-owned binding credentials, not review cards
or run history.

The plugin uses Python 3.11+, `mcp==1.27.0`, and stdio transport owned by Codex
Desktop. It opens no network listener.
