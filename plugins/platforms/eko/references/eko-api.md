# Eko Messaging API Reference

Quick local summary of the stable Eko Messaging API docs used by the Hermes Eko platform.

For full plain-markdown copies, see:

- `raw_md/master.md`
- `raw_md/messaging-api_getting-started.md`
- `raw_md/messaging-api_webhook-api.md`
- `raw_md/messaging-api_replying-message.md`
- `raw_md/messaging-api_sending-a-message.md`
- `raw_md/messaging-api_managing-chat.md`

## Scope

This reference covers the Messaging API pages that matter for the adapter:

- Getting Started
- Webhook API
- Replying Message
- Sending a Message
- Managing Chat

## Overview

The Bot API supports three core functions:

- user query
- chat management
- message creation

It can be used for one-way notification bots and interactive bots.

## Authentication

- Create a bot profile in the Eko Admin Panel.
- Use OAuth2 client-credentials.
- Required scope: `bot`.
- Fetch an access token from `POST /oauth/token`.
- Send `Authorization: Bearer <access_token>` on API requests.

## Webhook events

Eko sends webhook events to your configured HTTPS endpoint.

Common event types:

- `join` — a user opens a 1:1 chat with the bot for the first time
- `message` — a user sends a message to the bot

Common webhook fields:

- `replyToken` — used for reply endpoints
- `source` — user identity
- `message` — message metadata and content
- `timestamp` — event time

Example `join` event:

```json
{
  "events": [
    {
      "replyToken": "...",
      "type": "join",
      "source": {
        "type": "direct_chat",
        "uid": "..."
      },
      "timestamp": 1569563054859
    }
  ]
}
```

Example `message` event:

```json
{
  "events": [
    {
      "replyToken": "...",
      "type": "message",
      "source": {
        "type": "user",
        "userId": "...",
        "username": "..."
      },
      "message": {
        "id": "...",
        "type": "text",
        "groupId": "...",
        "topicId": "...",
        "text": "hello"
      },
      "timestamp": "2018-10-19T03:46:07.866Z"
    }
  ]
}
```

## Replying message

Reply endpoints use `multipart/form-data` except quick replies, which use JSON.

### Text reply

`POST /bot/v1/message/text`

Form fields:

- `message` — reply text
- `replyToken` — reply token from the webhook

### Picture reply

`POST /bot/v1/message/picture`

Form fields:

- `file` — uploaded image
- `replyToken` — reply token from the webhook

### Quick reply

`POST /bot/v1/message/quickreply`

JSON body:

- `replyToken`
- `message.data` — prompt text
- `message.meta.quickreply.template` — e.g. `default`
- `message.meta.quickreply.items[]` — quick-reply options with `type`, `value`, and `data.text`

## Sending a message

Successful send endpoints return a simple status payload such as:

```json
{ "status": "success" }
```

### Direct message

Text:

`POST /bot/v1/direct/message`

- Content-Type: `application/json`
- Body: `uid`, `message.type = "text"`, `message.data`

Picture:

`POST /bot/v1/direct/picture`

- Content-Type: `multipart/form-data`
- Fields: `uid`, `file`, optional `caption`

File:

`POST /bot/v1/direct/file`

- Content-Type: `multipart/form-data`
- Fields: `uid`, `file`

### Group/topic message

Text:

`POST /bot/v1/group/message`

- Content-Type: `application/json`
- Body: `gid`, `tid`, `message.type = "text"`, `message.data`

Picture:

`POST /bot/v1/group/picture`

- Content-Type: `multipart/form-data`
- Fields: `gid`, `tid`, `file`, optional `caption`

File:

`POST /bot/v1/group/file`

- Content-Type: `multipart/form-data`
- Fields: `gid`, `tid`, `file`

## Managing chat

### Query users

`GET /bot/v1/users?username=...`

Returns a list of users with fields such as:

- `_id`
- `username`
- `email`
- `firstname`
- `lastname`
- `deleted`

### Group and topic IDs

Eko group and topic IDs can be read from the Eko URL:

- Open the group chat and read the group ID and general topic ID from the URL.
- Click a specific topic to update the URL and read that topic ID.

### Create a group chat

`POST /bot/v1/groups`

- Content-Type: `multipart/form-data`
- Fields: `uids`, optional `name`, optional `file` group avatar

Response includes a `group` object with fields such as `_id`, `name`, `type`, `settings`, and `userCount`.

### Create a topic

`POST /bot/v1/groups/{group_id}/topics`

- Content-Type: `application/json`
- Body: `name`

Response includes a `topic` object with fields such as `_id`, `name`, and `gid`.

## Source pages

- Getting Started: https://eko.gitbook.io/api/messaging-api/getting-started
- Webhook API: https://eko.gitbook.io/api/messaging-api/request-from-eko
- Replying Message: https://eko.gitbook.io/api/messaging-api/response-to-be-sent-to-eko
- Sending a Message: https://eko.gitbook.io/api/messaging-api/sending-a-message
- Managing Chat: https://eko.gitbook.io/api/messaging-api/querying-user
