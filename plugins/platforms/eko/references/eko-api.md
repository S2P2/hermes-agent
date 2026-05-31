# Eko Messaging API Reference

Local snapshot of the stable Eko Messaging API docs used by the Hermes Eko platform.

## Scope

This reference covers the Messaging API pages that matter for the adapter:

- Getting Started
- Webhook API
- Replying Message
- Sending a Message
- Managing Chat

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
- `message`

## Sending a message

### Direct message

Text:

`POST /bot/v1/direct/message`

JSON body:

- `uid`
- `message.type = "text"`
- `message.data`

Picture:

`POST /bot/v1/direct/picture`

Multipart fields:

- `uid`
- `file`
- optional `caption`

File:

`POST /bot/v1/direct/file`

Multipart fields:

- `uid`
- `file`

### Group/topic message

Text:

`POST /bot/v1/group/message`

JSON body:

- `gid`
- `tid`
- `message.type = "text"`
- `message.data`

Picture:

`POST /bot/v1/group/picture`

Multipart fields:

- `gid`
- `tid`
- `file`
- optional `caption`

File:

`POST /bot/v1/group/file`

Multipart fields:

- `gid`
- `tid`
- `file`

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

### Create a group chat

`POST /bot/v1/groups`

Multipart fields:

- `uids` — repeated member IDs
- optional `name`

### Create a topic

`POST /bot/v1/groups/{group_id}/topics`

JSON body:

- `name`

## Source pages

- Getting Started: https://eko.gitbook.io/api/messaging-api/getting-started
- Webhook API: https://eko.gitbook.io/api/messaging-api/request-from-eko
- Replying Message: https://eko.gitbook.io/api/messaging-api/response-to-be-sent-to-eko
- Sending a Message: https://eko.gitbook.io/api/messaging-api/sending-a-message
- Managing Chat: https://eko.gitbook.io/api/messaging-api/querying-user
