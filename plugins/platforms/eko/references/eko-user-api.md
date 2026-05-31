# Eko User API Reference

Local snapshot of the stable Eko User API docs used by the Hermes Eko platform.

## Scope

This reference covers user provisioning and authentication integration.

## What it is

- Automates user provisioning and authentication in Eko.
- Supports two integration modes:
  - Active Directory connector
  - RESTful API

## Active Directory connector

- EkoADC is Eko's proprietary connector for user synchronization and authentication.
- It works with Microsoft Active Directory and other LDAP-compatible systems.
- It can run on-premise or in the cloud.

## User sync and auth

- User synchronization can use LDAP.
- CSV over FTP/SFTP is also described for profile synchronization.
- Authentication passes the user account to EkoADC over HTTPS.
- EkoADC performs LDAP binding and returns the result to Eko.

## Source pages

- Getting Started: https://eko.gitbook.io/api/user-api/getting-started
- AD Integration: https://eko.gitbook.io/api/user-api/ad-integration
