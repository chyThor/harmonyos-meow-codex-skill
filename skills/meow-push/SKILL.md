---
name: meow-push
description: Send concise MeoW notifications for user-authorized task milestones or explicitly requested results. Use when the user asks for MeoW progress, checkpoint records, or a notification at a specified node; ordinary completion alone does not authorize extra messages.
---

# MeoW checkpoints

This is an active tool, separate from automatic completion hooks. Keep routine subagent completion silent; the coordinating agent sends only the checkpoints the user requested.

## Use

Verify the authorized checkpoint has actually occurred, then run `python <skill-directory>/scripts/meow_push.py status`. It reports readiness without revealing the receiver or address. If unconfigured, follow [references/setup.md](references/setup.md); never ask for secrets in chat.

Create a small UTF-8 JSON payload outside the skill directory:

```json
{
  "project": "sample-task",
  "stage": "数据检查完成",
  "message": "输入结构已验证，可以继续处理。",
  "event_id": "unique-task-id:input-check:v1"
}
```

Use a stable ID for each logical checkpoint; different tasks or nodes need different IDs. Keep the summary minimal: omit receiver details, push addresses, tokens, private paths and full logs.

Send with `python <skill-directory>/scripts/meow_push.py send --payload <payload-file> --authorized`. The flag acknowledges the user's authorization for this checkpoint; selecting the skill alone is not authorization.

Read the JSON result: `sent` means API acceptance, `duplicate` means this checkpoint was already accepted, and `delivery_unknown` means stop and report uncertainty. Do not automatically repeat unknown deliveries. API acceptance does not prove phone display.

`dry-run --payload <payload-file>` validates formatting without network access and reports counts, not content. The script reads private configuration itself. Do not open, print, copy or include that configuration in transcripts or artifacts. Do not construct the notification URL yourself.

An intermediate result is a checkpoint, not completion of the whole goal. If automatic notifications already cover the final reply, add a final skill notification only when requested. No milestone authorization means no active send.

## Dependencies and privacy

Python 3.10+, standard library only. Windows uses current-user encryption. `MEOW_PUSH_CONFIG` can select an external private file for another environment or isolated tests; it must remain outside skill directories. Runtime state also stays outside the skill.

Share only this skill folder. It contains no personalized receiver, address, configuration, task history or credentials. Each user performs local setup; encrypted configuration is not transferable by copying it.
