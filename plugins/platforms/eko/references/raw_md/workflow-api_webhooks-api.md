# Webhooks API

Webhooks API allow you to subscribe to events that are happening in a Workflow. Rather than making an API call, Eko can send an HTTP request to an endpoint that you configure when an event happens. You can configure which workflow template is subscribed in Admin Panel.

## Setting up webhooks URL

You can manage your URL via workflow configuration page in your Admin Panel:

1. In workflow menu, Select workflow you want to subscribe.

> Screenshot replacement: Workflow menu with the workflow/template selected.


&#x20;   2\. Select Webhooks in Settings in to manage your endpoint

> Screenshot replacement: Workflow **Settings** page with **Webhooks** selected.


&#x20;   3\. This screen provides an interface you can use to set the Webhooks URL

> Screenshot replacement: Webhooks settings page where the endpoint URL is configured.

```mermaid
flowchart LR
    A[Workflow menu] --> B[Select workflow]
    B --> C[Settings]
    C --> D[Webhooks]
    D --> E[Set endpoint URL]
```


## Selecting Content Variety

You can select content that you want to subscribe by define the Api key tag in each content in a workflow.

{% hint style="warning" %}
Only the variety that is defined API tag will be sent with the webhooks request
{% endhint %}

In order to select content, go to the workflow template and click on "more setting" on the content you want to subscribe.

> Screenshot replacement: Workflow builder content item with **more setting** menu.


You can define the name of API tag. This tag will be sent with the request.

> Screenshot replacement: Content settings where the API tag is defined; only tagged content appears in webhook payloads.

```mermaid
flowchart LR
    A[Workflow template] --> B[Content item]
    B --> C[More setting]
    C --> D[Define API tag]
    D --> E[Webhook payload includes tagged content]
```


## Webhooks request

Request will be posted to the endpoint URL you specify in Webhooks Settings. Data is signed with JWT and sent in the request payload under the key ‘data’ as following.

```
{
  "data": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJtZXRhIjp7IndvcmtmbG93Ijp7Il9pZCI6IjVkMDA5ZmZkNzQ0MzdhMmYyZWM4ZTI2MSIsInN0YXR1cyI6ImNvbXBsZXRlZCIsIm5ldHdvcmtJZCI6IjUxOTkwOWU4MGQ3NzM1YWI3YzAwMDAwMiIsImNyZWF0ZWRBdCI6IjIwMTktMDYtMTIgMDY6NDc6MjUuODY3WiIsImxhc3RBY3Rpdml0eSI6IjIwMTktMDYtMTIgMDc6MDE6MDguNDk2WiJ9fSwiZGF0YSI6W3siUmV2aWV3LlRpdGxlIjp7ImxhYmVsIjoiMzYwIERlZ3JlZSBQZXJmb3JtYW5jZSBSZXZpZXcgU2Vzc2lvbiIsInZhbHVlIjoiRm9yIEVuZ2luZWVycyJ9fSx7IlJldmlldy5QZXJpb2QiOnsibGFiZWwiOiJSZXZpZXcgUGVyaW9kIiwidmFsdWUiOiJZZWFybHkifX1dfQ.lUC_14-Cb_7z11kN4oWSqxgUn4OCLOolF-Mny9CAjk0"
}
```

You can use JWT decode with shared key we provide to see and verify the content. After decoding, you can expect the result as:

```
{
 "meta": {
  "workflow": {
   "_id": "5d009ffd74437a2f2ec8e261",
   "status": "completed",
   "networkId": "519909e80d7735ab7c000002",
   "createdAt": "2019-06-12 06:47:25.867Z",
   "lastActivity": "2019-06-12 07:01:08.496Z"
  }
 },
 "data": [
  {
   "Review.Title": {
    "label": "360 Degree Performance Review Session",
    "value": "For Engineers"
   }
  },
  {
   "Review.Period": {
    "label": "Review Period",
    "value": "Yearly"
   }
  }
 ]
}
```

[https://www.jsonwebtoken.io](https://www.jsonwebtoken.io/)
