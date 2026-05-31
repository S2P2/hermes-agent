# Eko Workflow API Reference

Local snapshot of the stable Eko Workflow API docs used by the Hermes Eko platform.

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

### Common fields

- `sender`
- `templateId`
- `inputs`

### Notes

- Priority values: High, Medium, Low
- Due date format: `YYYY-MM-DD` or `YYYY-MM-DDTHH:mm`
- Workflow inputs can include text, text areas, users, numbers, choices, dates, and time fields.

## Workflow webhooks

- Configure webhook URL in the workflow settings in the Admin Panel.
- Only content with an API tag is included in webhook payloads.
- Webhook payloads are signed as JWT and sent under `data`.
- The receiving app can decode and verify the JWT with the shared key.

## Source pages

- Getting Started: https://eko.gitbook.io/api/workflow-api/getting-started
- Create Workflow: https://eko.gitbook.io/api/workflow-api/create-workflow
- Webhooks API: https://eko.gitbook.io/api/workflow-api/webhooks-api
