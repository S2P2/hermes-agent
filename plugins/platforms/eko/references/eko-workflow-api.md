# Eko Workflow API Reference

Quick local summary of the stable Eko Workflow API docs used by the Hermes Eko platform.

For full plain-markdown copies, see:

- `raw_md/workflow-api_getting-started.md`
- `raw_md/workflow-api_create-workflow.md`
- `raw_md/workflow-api_webhooks-api.md`

## Scope

This reference covers workflow creation and workflow webhooks.

## What it is

- Send a workflow template to a specific Eko user.
- Create workflow instances from a template created in the Admin Panel.
- Subscribe to workflow events via webhook.

## Workflow creation

- Create a workflow template in the Admin Panel first.
- Use the template ID to create a workflow instance.
- Inputs are mapped with API tags configured in the workflow builder.
- Data can be inserted at the Start stage only.

### Request

- `POST /api/workflow/v1`
- Auth: OAuth access token
- Content-Type: `application/json`

### Common fields

- `sender` — username of sender
- `templateId` — workflow template ID
- `priority` — High, Medium, Low
- `dueDate` — `YYYY-MM-DD` or `YYYY-MM-DDTHH:mm`
- `inputs` — object keyed by API tags

### Input notes

Workflow inputs can include text, text areas, users, numbers, choices, dates, and time fields. The raw docs show examples for:

- `text`
- `textArea`
- `singleUser`
- `multiUsers`
- `number`
- `yesNo`
- `singleChoice`
- `multiChoice`
- `dateTime`
- `time`

### Response shape

Successful creation returns fields such as:

- `refId`
- `title`
- `createdAt`
- `template.id`
- `template.name`
- `sender`

## Workflow webhooks

- Configure webhook URL in the Admin Panel path: Workflow → Settings → Webhooks.
- Select content by defining API tags on workflow content.
- Only content with an API tag is included in webhook payloads.
- Webhook payloads are signed as JWT and sent under `data`.
- The receiving app can decode and verify the JWT with the shared key.

Decoded webhook data includes:

- `meta.workflow._id`
- `meta.workflow.status`
- `meta.workflow.networkId`
- `meta.workflow.createdAt`
- `meta.workflow.lastActivity`
- `data[]` entries keyed by API tag

## Source pages

- Getting Started: https://eko.gitbook.io/api/workflow-api/getting-started
- Create Workflow: https://eko.gitbook.io/api/workflow-api/create-workflow
- Webhooks API: https://eko.gitbook.io/api/workflow-api/webhooks-api
