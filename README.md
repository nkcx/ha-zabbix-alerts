# Zabbix alerts for Home Assistant

[![Validate](https://github.com/nkcx/ha-zabbix-alerts/actions/workflows/validate.yml/badge.svg)](https://github.com/nkcx/ha-zabbix-alerts/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

A companion to the [Zabbix integration (ha-zabbix)](https://github.com/nkcx/ha-zabbix).
It sets up Zabbix to notify Home Assistant **the moment** a problem starts, resolves
or is updated, and keeps that setup up to date. With it, ha-zabbix's problem events
and entities update within seconds instead of on the next poll.

*Independent community project, not affiliated with Zabbix SIA; see
[License and trademarks](#license-and-trademarks).*

## How it works

- **ha-zabbix** receives the alerts: a webhook that triggers an immediate refresh
  from the Zabbix API (**Configure → Alerts**).
- **This integration** configures Zabbix to call that webhook. It creates five
  objects:

| Zabbix object | Name | Purpose |
|---|---|---|
| Media type (webhook) | *Home Assistant (ha-zabbix-alerts)* | Sends the alert to Home Assistant with the shared secret |
| User role | *Home Assistant alerts (ha-zabbix-alerts)* | Type *User* with **no** frontend, API or action access |
| User group | *Home Assistant alerts* | **Read** permission on every host group: Zabbix only alerts users who can see the host. No frontend access |
| User | `ha-zabbix-alerts` | The recipient, with that role and the webhook URL as its media |
| Trigger action | *Home Assistant alerts (ha-zabbix-alerts)* | Notifies the user on problem, recovery and update, for every trigger, including suppressed problems and symptoms |

It checks them at startup and every hour and fixes any drift: the webhook script
after an update, the URL or secret, and read permission on host groups added
since. **It only ever touches objects it created.** If one of these names is
already taken by an object it didn't create, it stops with a repair issue instead
of taking it over. **Removing the integration deletes the five objects again.**

## Requirements

- [ha-zabbix](https://github.com/nkcx/ha-zabbix) **0.3.0** or newer, with
  **Configure → Alerts → Receive Zabbix alerts** turned on.
- Zabbix 7.0 or newer.
- An API token of a Zabbix **Super admin**, because only Super admins can create
  media types. Only this integration uses it; ha-zabbix keeps its own, more limited token.
- The Zabbix server must be able to reach Home Assistant's webhook URL.

## Installation

1. In HACS, add `https://github.com/nkcx/ha-zabbix-alerts` as a custom repository
   (type **Integration**), install **Zabbix alerts**, and restart Home Assistant.
2. **Settings → Devices & services → Add integration → Zabbix alerts**.
3. Choose the Zabbix server (your ha-zabbix entry) and enter the Super admin token.
4. Confirm the **webhook URL**, the address the Zabbix server uses to reach Home
   Assistant. The default is based on Home Assistant's internal URL. Change it if
   Zabbix needs a different address.

To check it works, open a problem in Zabbix, e.g. by disabling an agent. The
ha-zabbix **Last alert** sensor on the *Zabbix* device updates, and the problem
event fires within seconds.

**Reconfigure** changes the webhook URL; **Reauthenticate** replaces the token.

## Manual setup

If you'd rather not give Home Assistant a Super admin token, create the same
objects yourself:

1. In ha-zabbix, turn on **Configure → Alerts** and note the **URL** and **secret**.
2. Import [`zabbix/media_type_home_assistant.yaml`](zabbix/media_type_home_assistant.yaml)
   under **Alerts → Media types → Import**. Open the media type and set the `Secret`
   parameter to the secret.
3. Create a user role of type *User* with frontend, API and action access turned
   off, a user group with **Read** permission on the host groups you want alerts
   for (frontend access disabled), and a user with that role in that group. Give the user a media of type
   *Home Assistant* with **Send to** = the URL.
4. Create a **trigger action** (Alerts → Actions → Trigger actions) that sends a
   message to that user via *Home Assistant* in **Operations**, **Recovery
   operations** and **Update operations**. Turn off *Pause operations for suppressed
   problems* and *Pause operations for symptom problems* so Home Assistant hears
   about everything.

## Troubleshooting

- **Nothing arrives:** check **Reports → Action log** in Zabbix. The webhook's
  error (e.g. *Home Assistant answered HTTP 401*, or a connection error) is shown
  there.
  - 401: the secret doesn't match. This integration fixes it at the next hourly check; reload it to fix it now.
  - A connection error: the URL isn't reachable from the Zabbix server.
- **Repair issue "Zabbix already has a … named …":** an object with one of the
  names above exists but wasn't created here. Rename or delete it; the setup is
  retried every hour.
- **Diagnostics** (integration ⋮ menu) show the Zabbix object ids, the last run,
  and the last error. The token and URL are redacted.

## Removal

**Settings → Devices & services → Zabbix alerts → ⋮ → Delete.** This deletes the
media type, user role, user group, user and action from Zabbix. If Zabbix can't be reached
at that moment, the log lists their ids so you can delete them by hand. Then
remove the repository from HACS and, if you like, turn off **Alerts** in ha-zabbix.

## Development

The integration imports ha-zabbix's API client. For development, link or copy
ha-zabbix's `custom_components/zabbix` into `custom_components/`; CI checks out
ha-zabbix for this.

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest tests
```

After changing the media type, regenerate the manual-setup file with
`python scripts/generate_media_type.py`. CI's contract tests provision everything
on stock Zabbix 7.0 and 7.4 and deliver real problem, update and recovery alerts.

## License and trademarks

This integration is licensed under the [MIT License](LICENSE).

This is an independent, community project. It is **not affiliated with, endorsed
by, or sponsored by Zabbix SIA**. Zabbix® and the Zabbix logo are trademarks of
Zabbix SIA and are used here only to identify the software this integration works
with. All other trademarks are the property of their respective owners.
