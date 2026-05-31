# Eko User API Reference

Quick local summary of the stable Eko User API docs used by the Hermes Eko platform.

For full plain-markdown copies, see:

- `raw_md/user-api_getting-started.md`
- `raw_md/user-api_ad-integration.md`

## Scope

This reference covers user provisioning and authentication integration.

The downloaded raw docs include Getting Started and AD Integration. They mention RESTful API as another integration mode, but no separate RESTful User API page is present in `raw_md/`.

## What it is

- Automates user provisioning and authentication in Eko.
- Supports two integration modes:
  - Active Directory connector
  - RESTful API

## Active Directory connector

- EkoADC is Eko's proprietary connector for user synchronization and authentication.
- It works with Microsoft Active Directory and other LDAP-compatible systems.
- It can run on-premise or in the cloud.
- It is responsible for user synchronization and user authentication.

## User sync and auth

- User synchronization can use LDAP.
- CSV over FTP/SFTP is also described for profile synchronization.
- CSV/file sync should be used for profile sync only; LDAP-compatible directory software is still required for authentication.
- Authentication passes the user account to EkoADC over HTTPS.
- EkoADC performs LDAP binding and returns the result to EkoIDMAPI.

## EkoADC server specs

- CPU: 4+ cores
- Memory: 4+ GB
- Disk: 100+ GB
- Network: 1 Gb Ethernet with NAT
- OS: Ubuntu 20.04 LTS

## Access control summary

| Source | Destination | Ports |
| --- | --- | --- |
| EkoADC | Internet | TCP/443 |
| Internet | EkoADC | TCP/443 |
| EkoADC | Customer AD | LDAP / LDAPS |
| EkoADC | Time server | NTP TCP/123, UDP/123 |
| EkoADC | DNS server | DNS UDP/53 |
| EkoADC | FTP server | FTP / FTPS |

## Install / upgrade pointer

The raw AD Integration doc includes the detailed Docker Compose setup, `.env`, `config.json`, and upgrade commands. Keep those details in `raw_md/user-api_ad-integration.md`; use this summary only for quick orientation.

## Source pages

- Getting Started: https://eko.gitbook.io/api/user-api/getting-started
- AD Integration: https://eko.gitbook.io/api/user-api/ad-integration
