# Mobile Connector

![Odoo 18.0](https://img.shields.io/badge/Odoo-18.0-714B67) ![License LGPL-3](https://img.shields.io/badge/license-LGPL--3-blue) ![Version 18.0.1.21.0](https://img.shields.io/badge/version-18.0.1.21.0-informational)

> Stable REST-like API, device tokens, form spec engine and push notifications for the MobileDoo app.

Odoo 18 addon of [MobileDoo](https://github.com/MobileDoo) (`mobile_connector`).

## Overview

`mobile_connector` is the server side of [MobileDoo](https://github.com/MobileDoo). It exposes one **stable, versioned HTTP API**
(`/mobile/api/v1`, currently API **1.19**) between the mobile app and Odoo, so the app never touches the ORM and the
same app talks to Odoo 17, 18, 19 and 20. Differences between Odoo releases are absorbed here, one branch per release.

The addon is a framework as much as a feature: it provides authentication, a server-side form engine, generic record services,
change detection, push notifications and the configurable home screen. Every other `mobile_*` addon (time off, expenses,
purchasing, inventory...) only declares *what* it exposes and plugs into these services.

## Features

- **Device-token authentication.** One token per device, a 256-bit random value of which only the SHA-256 is stored. Tokens have no
  expiry and are revoked on logout, by an administrator, when the user changes their password, or when the user is archived.
  TOTP two-factor accounts are supported. Browser session cookies are deliberately *not* accepted on mobile routes.
- **Server-side form spec engine** (`mobile.form.spec`). The server reads the *real* form view (inheritance and `groups` applied),
  evaluates `invisible` / `readonly` / `required` for the current record, runs the genuine `onchange()` and returns only the buttons
  the user may press. Custom fields, buttons, states, constraints and wizards from customer modules therefore work in the app without
  rebuilding it. Only buttons present in the returned spec can be executed.
- **Record services** (`mobile.record.service`). A feature declares a model, scopes, required capabilities and the fields of a list
  card, and receives `list`, `spec`, `onchange`, `save`, `action`, `name_search` and `attachment/add` routes with access control,
  optimistic concurrency (`409 conflict` on a stale `version`) and idempotent retries (`client_id`) for offline queues.
- **Change detection** (`/changes`). Records, lists and the dashboard carry an opaque `version` (microsecond `write_date` plus count).
  The app polls only while a screen needs it, every `mobile_connector.poll_interval` seconds, instead of using the Odoo bus.
- **Approval inbox and generic record viewer.** Every Odoo approval flow creates an activity; the inbox plus a viewer that shows a
  record's real buttons give one place to approve across all modules, including custom ones.
- **Generic form with editable lines** (`/form/*`, `full: true`) for models that have no native screen, including one2many lines,
  statusbar, images and signatures.
- **Odoo menus on the phone.** Every list-type menu the user has on the web, including custom modules, is browsable with the
  action's domain, search-view filters and record rules.
- **Configurable home screen**: feature tiles, KPI cards (computed with the *viewer's* rights), quick actions, record lists,
  per-role layouts, company announcements and per-user customisation, all managed in Odoo without an app release.
- **Management overview** (`/dashboard`): cards declared by each installed module, every number computed with the viewer's rights.
- **Notification centre**: the user's Odoo Inbox, with read state shared with the web client.
- **Push notifications** through a company-owned Firebase project (Android, free) or a push gateway (Android and iPhone, with
  end-to-end encryption of the payload on iPhone).
- **Branding and localisation**: company logo and login background, app strings for English and Vietnamese (built into the app) plus 8
  more languages served by the server from `app_i18n/`, and date / time formats read from each Odoo language.
- **Self-documenting**: OpenAPI 3.1 spec at `/mobile/api/v1/openapi.json` and Swagger UI at `/mobile/api/docs`.

## Requirements

| | |
|---|---|
| Odoo | 18.0 (Community or Enterprise) |
| Odoo modules | `base`, `mail`, `base_setup` |
| Python packages | `requests` |
| Client | the [MobileDoo](https://github.com/MobileDoo) app for Android and iPhone |

This branch targets **Odoo 18** only; use the branch of your Odoo version.

## Installation

```bash
git clone --branch 18.0 https://github.com/MobileDoo/mobile_connector.git
# add the cloned folders to addons_path in odoo.conf, then:
odoo -d <database> -i mobile_connector --stop-after-init
```

The addon is the `mobile_connector/` folder at the root of the repository, so the repository folder itself can go on `addons_path`.

`mobile_connector` is **not** installed automatically: install it from *Apps* where the company needs it.

To upgrade after pulling a newer version: `odoo -d <database> -u mobile_connector --stop-after-init`.

## How it works

### Request conventions

| | |
|---|---|
| Method / body | `POST` with a JSON body (`/info` also accepts `GET`) |
| Authentication | `Authorization: Bearer <device_token>` (not needed for `/info` and `/auth/login`) |
| Company | `X-Mobile-Company-Ids: 1,3` (first id is the current company) |
| Language | `X-Mobile-Lang: vi_VN`: labels and messages in that language when it is enabled in Odoo, else the user's language |
| Success | `{"ok": true, "data": ...}` |
| Error | `{"ok": false, "error": {"code": "...", "message": "..."}}` with an HTTP 4xx/5xx |

Error codes: `bad_request`, `user_error` (Odoo `UserError` / `ValidationError`, message shown verbatim), `invalid_credentials`,
`totp_required`, `totp_invalid`, `access_denied`, `access_error`, `not_internal_user`, `feature_denied`, `missing`, `conflict`,
`server_error`. An invalid or revoked token returns HTTP 401; the app treats any 401 as an expired session.

Mobile routes use a custom `auth='mobile'` method (see `models/ir_http.py`): the device token is resolved to a user, and the request
then runs **as that user** with their groups, access rights, record rules, companies and language. Nothing here uses `sudo()` to
widen what a user can read.

### Connecting an app

1. The app probes `/web/webclient/version_info`, lists databases (`/web/database/list`, or asks for the name when `list_db` is off)
   and selects one with `/web/session/authenticate` when the server hosts several.
2. `POST /mobile/api/v1/auth/login` with `login`, `password`, optional `totp_code` and a `device` object returns the `device_token`.
3. Alternatively *Settings > Mobile > Connect the app* shows a QR code `odoomobile://connect?url=<server>&db=<database>`
   (no secret inside). The address comes from `mobile_connector.public_url`, else `web.base.url`.

### Endpoints of the core

| Endpoint | Purpose |
|---|---|
| `/info`, `/auth/login`, `/auth/logout`, `/me` | Server discovery (features, languages, push provider, policy), sign-in, session and the user's `access` map |
| `/changes` | Which of the watched records / lists changed (max 100 each per call) |
| `/records/<service>/{list,spec,onchange,save,action,name_search,attachment/add}` | Generic record service of every feature |
| `/form/{spec,onchange,save,action,stage,note,name_search}`, `/form/line/*` | Generic viewer and editable form for any model; log note and next activity on any record |
| `/wizard/{spec,execute}` | Odoo wizards opened by a button |
| `/activities/{list,done}` | Approval inbox |
| `/notifications/{list,mark_read}`, `/device/push_token` | Notification centre and push registration |
| `/menus`, `/menus/list` | The user's Odoo apps and the records of any list menu |
| `/home`, `/home/catalog`, `/home/preferences`, `/home/announcements/dismiss` | Configurable home screen |
| `/dashboard` | Management overview cards |
| `/i18n`, `/logo`, `/login_background`, `/image/<model>/<id>/<field>` | App strings, branding and record images |
| `/openapi.json`, `/mobile/api/docs` | Machine- and human-readable API description |

### Features and capabilities

A feature (`timeoff`, `purchase`, `stock`...) is declared by the addon that implements it. `mobile.api._mobile_access()` returns, per
user, the capabilities they hold (for example `{'purchase': {'view': True, 'approve': False}}`), computed from Odoo groups and access
rights. It drives the app's navigation (`/me` -> `access`) **and** is enforced on the server: a route called without the capability
answers `403 feature_denied`. Hiding something in the UI is never the only protection. The list of installed features is returned by
`/info`, which is how an app learns what a given server supports.

### Data model

| Model | Role |
|---|---|
| `mobile.device` | One row per signed-in device: token hash, user, platform, app version, push token and its source, last seen |
| `mobile.push.message` | Outgoing push queue: state (`pending`, `queued`, `skipped`, `failed`...), retry counter, delivery receipts from the gateway |
| `mobile.home.item`, `mobile.home.layout(.line)`, `mobile.home.preference` | Home screen configuration: items, role layouts, personal choices |
| `mobile.announcement` | Company announcements shown on the home screen |
| `mobile.field.config` | Extra fields shown on cards and in the generic viewer for a model |
| `mobile.request.log` | Replay guard: the stored answer of a request carrying a `client_id`, so a request the phone sends twice (offline queue, timeout) is applied once. Vacuumed automatically |
| `mobile.api`, `mobile.record.service`, `mobile.form.spec`, `mobile.record.viewer`, `mobile.*.service` | Abstract services: the extension surface (not tables) |

## Configuration

Administrators find everything under **Settings > Mobile** and in the **Mobile App** menu (Settings, Announcements, Home Screen,
Role Layouts, Personal Home Screens, Extra Fields, Devices, Push Messages). Backing system parameters:

| Parameter | Meaning |
|---|---|
| `mobile_connector.public_url` | Address written in the connection QR code (default `web.base.url`) |
| `mobile_connector.poll_interval` | Seconds between change-detection polls while a screen is open (default 20) |
| `mobile_connector.push_provider` | `none`, `firebase` (own project, Android) or `gateway` |
| `mobile_connector.gateway_url`, `.gateway_token` | Push gateway address and site token |
| `mobile_connector.push_activity`, `.push_inbox` | Push for assigned activities / for Inbox messages (both on by default) |
| `mobile_connector.require_app_lock` | Require biometric / device-credential unlock in the app |
| `mobile_connector.menu_browser` | Let users browse Odoo menus in the app |

Scheduled actions: *send push notifications* (every 5 minutes), *read push results from the gateway* (every minute) and *report to the
push gateway* (every 6 hours; the gateway uses it to count the accounts that receive notifications).

## Security

- Configuration models are readable and writable by **system administrators only** (`security/ir.model.access.csv`); users never
  see other people's devices, push messages or home configuration.
- Every API call runs with the signed-in user's own rights. KPI and dashboard figures are computed with the *viewer's* record rules.
- Device tokens are stored hashed; the plain token exists only on the phone (Keychain / Keystore).
- Only buttons returned in a spec can be run, so the API cannot be used to call an arbitrary method.
- Push payloads never carry business amounts; on iPhone the payload is end-to-end encrypted through the gateway.
- Reverse proxies must allow request bodies large enough for photos (and videos for delivery proof); `nginx`: `client_max_body_size 60m;`.

## Push notifications

*Settings > Mobile > How push notifications are sent*:

| Mode | Path | Devices | Cost |
|---|---|---|---|
| `firebase` | Odoo calls FCM HTTP v1 itself with the company's service-account key (JWT signed on the server) | Android only | Free |
| `gateway` | Odoo queues to a push gateway, which delivers to FCM and APNs | Android and iPhone | Paid service |
| `none` | | | |

Each push carries `{site_id, model, res_id, type, message_id}`, so a tap opens the right site and record, from any app state. Tokens
reported invalid by FCM are deleted. A phone receives push from one configuration at a time; moving to a server with a different
provider resets its notification settings (the app warns first).

## Extending

Customer modules extend the app **without changing it** wherever possible:

| Need | How | Code? |
|---|---|---|
| Show extra existing fields | *Mobile App > Extra Fields* | No |
| New field with show/hide/required rules | Add it to the model and form view; list it in `_mobile_extra_fields` | Python + XML |
| New button, state or approval flow | Add a button to the form `<header>`; new states appear through `state_selection` | Python + XML |
| Hide, rename or add a button for mobile only | Override `_mobile_buttons` (and `_mobile_run_button` for `type: "mobile"`) on the feature's service | Python |
| Change a list or default values | Override `_mobile_domain(scope)` / `_mobile_new_context()` | Python |
| Push for a custom event | Override `_mobile_on_state_change` / `_mobile_push_text`, or call `mobile.push.message._enqueue()` | Python |
| Home tiles, quick actions, tabs, sections | Override `mobile.api._mobile_home_tiles()`, `_mobile_home_shortcuts()`, `_mobile_home_tabs()`, `_mobile_home_sections()` | Python |
| Dashboard card | Extend `mobile.dashboard.service._card_defs()`; grant `access['dashboard']` | Python |
| Custom module with no native screen | Appears automatically in the user's Odoo menus; hide with *Hide in Mobile App* on the menu item | No |
| Custom editor for a field | Return `widget: "<key>"`, register it in the app | TypeScript |

Minimal example: a customer module adding a field to `hr.leave` and showing it in the app (requires `mobile_hr_holidays`).

```python
class HrLeave(models.Model):
    _inherit = 'hr.leave'
    handover_user_id = fields.Many2one('res.users', string='Handover to')

class MobileTimeoffService(models.AbstractModel):
    _inherit = 'mobile.timeoff.service'

    @api.model
    def _mobile_extra_fields(self):
        return super()._mobile_extra_fields() + [{'name': 'handover_user_id', 'sequence': 10}]
```

The visibility, read-only and required rules of the field come from the form view (`required="number_of_days > 2"`), evaluated by the
server for each record; the app applies them without any code of its own.

## Compatibility

| Odoo | Branch | Notes |
|---|---|---|
| 17.0 | `17.0` | Expense reports exist (`hr.expense.sheet`) |
| 18.0 | `18.0` | Expense reports exist |
| 19.0 | `19.0` | Expenses are approved one by one; default branch |
| 20.0 | `20.0` | Same shape as 19.0 |

The branches carry the same addon with release-specific adaptations. The server and the app negotiate through `api_version` and the
feature list in `/info`, so an app release works with every supported Odoo release.

## Contributing

Issues and pull requests are welcome. Open pull requests against the branch of the Odoo version you are fixing (this one is `18.0`) and
keep the change inside the `mobile_connector/` folder.

These repositories are published from MobileDoo's development repository, which remains the source of truth. A merged pull request is
taken back there, and the branches of the other Odoo versions are updated by the maintainers, so a fix is usually welcome even if you can
only test it on one version. `README.md` and `LICENSE` are generated at each release; changes to them are not carried back.

## License

LGPL-3: see [LICENSE](LICENSE).
