# Private setup

Run `python <skill-directory>/scripts/meow_push.py setup` in a local interactive terminal. The service base address and recipient prompts hide input; do not paste them into chat. The script stores current-user encrypted configuration in the private application-data directory outside the skill.

`status` reports readiness without private values. `setup --from-json <external-file>` imports an explicitly authorized existing configuration silently. Supported names: `base_url` / `baseUrl`, `recipient` / `meowNickname`, optional `device_label`. `--device-label` sets a local device label; `--replace` explicitly replaces an existing private configuration. Do not copy the source file into the skill folder.

Windows uses DPAPI. Other systems may use an owner-only external JSON configuration selected with `MEOW_PUSH_CONFIG`; its fields are `base_url`, `recipient`, `device_label`, optional `timeout_seconds` (default 10) and `minimum_interval_seconds` (default 3). Enter actual values locally; examples intentionally contain neither an address nor recipient.

The transport sends GET requests with encoded recipient, title and message path segments. Use a service supporting that contract. Titles are `GPT(device)·project`; the body includes the checkpoint stage. Requests and response bodies are never printed. Redirects are blocked and uncertain deliveries are not retried automatically.

Report safe error codes if sending fails; do not inspect private configuration in chat. A recipient installs the skill and configures their own receiver separately.
