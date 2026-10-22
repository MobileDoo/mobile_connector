# Mobile Connector (`mobile_connector`)

Stable REST-like API, device tokens, form spec engine and push notifications for the MobileDoo app.

Odoo addon for [MobileDoo](https://github.com/MobileDoo), the mobile app for Odoo. This branch (`17.0`) is for **Odoo 17**; every supported Odoo version has its own branch with the same addon name.

This is the core addon: the other `mobile_*` addons depend on it and the MobileDoo app needs it on the Odoo server.

## Installation

Add this repository to `addons_path` (the addon is the `mobile_connector/` folder at the root) and install `mobile_connector` from *Apps*, or:

```bash
odoo -d <database> -i mobile_connector --stop-after-init
```

## License

LGPL-3 - see [LICENSE](LICENSE).
