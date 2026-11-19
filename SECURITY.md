# Security policy

The selected OpenCode executable runs with the Hermes process environment and working-directory configuration. Use the installed OpenCode executable and its trusted project configuration. OpenCode determines the model, provider, credentials and native tool permissions.

The bridge declines ACP requests for permission, filesystem access and terminal access. It converts text tool requests into Hermes tool calls for validation. Native actions already allowed by OpenCode follow the CLI's own permissions. Once OpenCode reports such an action as running or completed, the bridge fails the turn, but it cannot undo the action. Select the tool-less `hermes-relay` agent to prevent native actions.

Report suspected vulnerabilities privately to the maintainer of the deployed fork. Omit credentials, private prompts and internal logs.
