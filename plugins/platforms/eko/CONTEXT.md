# Eko Platform

The Eko Platform context describes the language Hermes uses when connecting Eko Messaging conversations to Hermes gateway sessions.

## Language

**Eko conversation**:
An Eko DM or Eko topic route that Hermes treats as one session. One **Eko conversation** maps to one Hermes session.
_Avoid_: Chat, channel, thread

**Eko DM**:
A direct Eko conversation between a user and the bot. An Eko DM is distinct from a group topic conversation.
_Avoid_: Direct channel, private channel, direct topic

**Eko group**:
An Eko conversation space that can contain user-created Eko topics. A group can contain many topics.
_Avoid_: Server, channel, workspace

**Eko topic**:
A user-created subdivision inside an Eko group. An **Eko topic** can define its own Eko conversation when paired with its group.
_Avoid_: Thread, channel, auto-topic

**Eko topic route**:
The address of an Eko conversation inside a group, identified by one group and one topic. A topic route is distinct from a DM conversation.
_Avoid_: Group route, thread route, channel route

**Eko reply token**:
A short-lived, single-use token attached to an inbound Eko message and preferred for the first outbound response to that Eko conversation.
_Avoid_: Auth token, session token, access token

**Eko push delivery**:
Outbound delivery to an Eko conversation when no usable Eko reply token is available. It is the fallback path for continuing a conversation after the first response window.
_Avoid_: Reply, token delivery, direct send

**Standalone Eko delivery**:
Outbound delivery to an Eko conversation initiated without an active inbound Eko message. A standalone delivery must name its target conversation explicitly.
_Avoid_: Non-gateway delivery, scheduled-only delivery, reply delivery

**Eko management tools**:
The separate agent-visible tools for creating Eko groups, creating Eko topics, and querying Eko users. Each management tool represents one management capability.
_Avoid_: Eko admin tools, Eko control tool, single management tool

**Management action allowlist**:
The gate that determines which Eko management capabilities are available to the agent. It allows or withholds management actions, not Eko conversations.
_Avoid_: User allowlist, group allowlist, permission list

**Eko access allowlist**:
The gate that determines which Eko users and Eko conversations may reach the agent. It is separate from the management action allowlist.
_Avoid_: Management allowlist, permissions, admin list

## Flagged Ambiguities

**Chat** is ambiguous in this context. Use **Eko conversation** for session identity, **Eko DM** for one-to-one conversations, **Eko group** for group spaces, and **Eko topic route** for group-plus-topic addressing.

## Example Dialogue

Developer: "Should this response go back to the same Eko conversation?"
Domain expert: "Yes. A message in an Eko DM stays in that DM; a message in an Eko topic stays in that topic route's conversation."

Developer: "What identifies the group topic conversation?"
Domain expert: "Use the Eko topic route: exactly one group and one topic."
