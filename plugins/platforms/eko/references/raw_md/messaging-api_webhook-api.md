# Webhook API

Webhooks allow developer to subscribe to events that are happening with Bot in Eko. Eko can send an HTTP request to an endpoint that you configure. There are 2 types of event including Create a chat and sending a message.

## User create a chat room

When a user create a 1-1 chat room with bot for first time, Eko will send an event to customer endpoint.

```
"root":
    "events":
        0:
        "replyToken": "d4c1027bafd76d1ed1ab08dd0ffd9cdd15608fcb"
        "type": "join"
        "source":
            "type": "direct_chat"
            "uid": "5d8af2ff164176ecaec49e5c"
        "timestamp": 1569563054859
```

## Message request

When the bot has sent a message, the endpoint will receive a request from the Eko server once the bot is sent a message as below

| Method                  | POST                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Request body sample** | <p><strong>{</strong></p><p>  <strong>events:</strong></p><p>    <strong>\[</strong></p><p>      <strong>{</strong></p><p>        <strong>replyToken: '8350939af2afb69a969649e1e8a943',</strong></p><p>        <strong>type: 'message',</strong></p><p>        <strong>source:</strong></p><p>          <strong>{</strong></p><p>            <strong>type: 'user',</strong></p><p>            <strong>userId: '5ac20cd38c62770001538ece'</strong></p><p>            <strong>username: 'u1.tutorial'</strong></p><p>          <strong>},</strong></p><p>        <strong>message:</strong></p><p>          <strong>{</strong></p><p>            <strong>id: '5bcaa50554ffc8d4b1a861b1',</strong></p><p>            <strong>type: 'text',</strong></p><p>            <strong>groupId: '5ae99669299892c81ec1d7fa',</strong></p><p>            <strong>topicId: '5ae996692998924badc1d7fb',</strong></p><p>            <strong>text: 'hello'</strong></p><p>          <strong>},</strong></p><p>          <strong>timestamp: '2018-10-19T03:46:07.866Z'</strong></p><p>      <strong>}</strong></p><p>    <strong>]</strong></p><p><strong>}</strong><br></p> |

**Request parameters**

| Name       | Type     | Description                                                     |
| ---------- | -------- | --------------------------------------------------------------- |
| replyToken | string   | reply token to acknowledge which message the bot is replying to |
| type       | string   | message type                                                    |
| source     | object   | information of the user who sent the message                    |
| message    | object   | message information and content                                 |
| timestamp  | datetime | time when the message was sent                                  |
