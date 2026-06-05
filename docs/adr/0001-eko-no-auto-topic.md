# Eko does not auto-create topics on new sessions

Eko sessions use user-created topics for isolation. We do not auto-create a new Eko topic on every inbound message (unlike Discord, which auto-creates a thread on each @mention). The Discord adapter does this because Discord server channels are shared spaces requiring threads for conversation isolation. Eko DMs and groups are already isolated — users create topics when they want a new conversation, which is the natural Eko workflow.

**Considered:** Auto-creating a topic per session (matching Discord's `_auto_create_thread` pattern).

**Why not:**
- Eko topics don't auto-archive (Discord threads expire after 24h). Auto-creating would flood chats with persistent bot-generated topics.
- Users already create topics manually. Each topic already gets its own Hermes session via `sessionId = {groupId}_{topicId}`. No extra mechanism needed.
- `direct_chat` groups are 1:1 chats. Auto-creating topics inside a DM feels unnatural — topics are meant for group workflows.
- The webhook reply token is for the original message. Auto-creating a new topic means the first response loses the free reply and must use push.

Agent-initiated topic creation (`eko_create_group`, `eko_create_topic` tools) is the right level of automation — the agent creates topics when there's a programmatic reason (delegation, kanban, notification routing), not automatically on every message.

**Status:** Accepted (2026-05-30)
