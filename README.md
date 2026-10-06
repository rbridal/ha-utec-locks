# U-tec Locks for Home Assistant

A community integration for U-tec / Ultraloq smart locks, using U-tec's OpenAPI and your own API credentials.

> **Status: 0.1.0 pre-release, for hardware testing.** Expect rough edges and report them in [issues](https://github.com/rbridal/ha-utec-locks/issues).

**Not made, endorsed, or supported by U-tec or Xthings.** Please report problems with this integration [here](https://github.com/rbridal/ha-utec-locks/issues), not to U-tec support.

## Is this integration for you?

**It is for you if Home Assistant is how you run your locks.** Your automations lock up at night and when you leave, your dashboard is where you check the doors, and you mostly leave the locks alone at the door. A shop door that Home Assistant puts into Passage mode during business hours is a perfect fit.

**It is not for you if you often lock or unlock at the door** (keypad, fingerprint, thumb-turn, key) **or often use the U-tec app.** U-tec can send Home Assistant a notification when a lock changes, but those notifications often don't arrive. When they don't, Home Assistant only finds out at its next check, every 30 seconds by default and sometimes longer when U-tec's servers are having trouble. During that gap Home Assistant can show "locked" when the door was just unlocked by hand. If that's how your household uses its locks, the U-tec app will serve you better, or a local connection (Matter or Z-Wave) if your model supports one.

Changes you make *from* Home Assistant are different: the integration checks on them right away and confirms them within seconds.

## At a glance

| | |
|---|---|
| Devices | U-tec / Ultraloq locks available through the U-tec OpenAPI. Locks only. |
| Talks to | U-tec cloud (`api.u-tec.com`), plus optional push notifications to your Home Assistant |
| Updates | Checks every 30 seconds (the minimum), plus push when it works, plus quick checks after your own commands |
| Credentials | Your own OpenAPI Client ID and Secret, from the Xthings Home app |
| Install | HACS (custom repository) |
| Requires | Home Assistant 2026.3 or newer |

## What you get

For each lock:

| Entity | What it shows |
|---|---|
| Lock | Locked, unlocked, locking, unlocking, jammed, or unknown. Lock and unlock. |
| Lock mode | Normal, Passage, or Locked (mode 2). See [Lock mode](#lock-mode-and-passage). |
| Door | Open or closed, on locks with a door sensor. |
| Battery | Low or normal. |
| Battery level | Critically low, low, medium, high, or full. U-tec reports five steps, not a percentage. |
| Battery (percent) | Off by default. The five steps shown as 20 to 100% for cards that need a number. |
| Status stale | On when Home Assistant hasn't heard from the lock for too long. |
| Cloud connection | Whether U-tec says the lock is online. |
| Last report | When the lock's state last came in. Off by default. |
| Command result | An event for each lock command: confirmed, not confirmed, or rejected. |

For your U-tec account: API requests (total, last 24 hours, last hour), projected requests per day, API errors, commands, confirmation checks, pushes received, push status, push healthy, last push, poll interval in use, and a button to re-register push. Totals survive restarts.

## Before you install

You need:

1. Home Assistant 2026.3 or newer, with [HACS](https://hacs.xyz/).
2. Your locks set up in the **Xthings Home** app (formerly U-tec / U home), version 3.5.5 or newer.
3. Your Home Assistant URL set under **Settings → System → Network**.
4. For push notifications (optional): Home Assistant Cloud (Nabu Casa), or an external **HTTPS** URL for your Home Assistant with a certificate from a trusted authority. Plain HTTP is not used for push. Without either, everything works on polling alone.

## Install

### HACS

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/rbridal/ha-utec-locks`, type **Integration**.
3. Find **U-tec Locks**, download it, and restart Home Assistant.

### Manual

Copy `custom_components/utec_locks` from the latest release into your `config/custom_components/` folder and restart Home Assistant.

## Set up

### 1. Get your OpenAPI credentials

1. Open the **Xthings Home** app and go to **My Account → OpenAPI**.
2. Turn on OpenAPI, choose your role, and **select the locks** you want Home Assistant to see. Locks you don't select here won't show up.
3. Set **Redirect URI** to exactly `https://my.home-assistant.io/redirect/oauth`. Don't change the hostname.
4. Make sure the scope is **OpenAPI** and save.
5. Note the **Client ID** and **Client Secret**.

### 2. Add the integration

1. **Settings → Devices & services → Add integration → U-tec Locks.**
2. Enter your Client ID and Client Secret when asked. (Home Assistant stores them under **Settings → Application credentials**.)
3. Sign in to U-tec and approve access.
4. Your locks appear. If none do, check step 1.2.

You can add more than one U-tec account. Each account is its own entry.

## How state updates work

- **Polling.** Every 30 seconds (you can choose longer, not shorter), Home Assistant asks U-tec for the state of all your locks in one request. Ten locks cost the same as one.
- **Push.** If push is set up and working, U-tec tells Home Assistant about changes as they happen. The integration keeps an eye on whether push is actually delivering changes (see [Push notifications](#push-notifications)), and it never stops polling, because push can stop without warning.
- **After your commands.** When you lock, unlock, or change mode from Home Assistant, it checks that lock up to 10 times over about 56 seconds (intervals 1/1/1/1/2/3/5/8/13/21 s) until the change shows up.
- **When it doesn't know.** If Home Assistant hasn't had a good report from a lock for 2 minutes (at the default interval), the lock shows **unknown** and **Status stale** turns on. The lock's attributes still show the last known state and when it was reported. The lock stays usable, so you can still lock and unlock it.

## Lock and unlock: what to expect

- **Commands always go out.** If you press Lock, the integration sends the lock command, even if Home Assistant thinks it's already locked, the state is stale, or U-tec says the lock is offline. It never skips a command because of what it last saw.
- **You see what the lock reports, not a guess.** Right after a command the lock shows **locking** or **unlocking**. It switches to locked or unlocked when the lock confirms it.
- **If the lock doesn't confirm.** After about 90 seconds the lock goes back to showing what U-tec last reported, a warning is logged, and the **Command result** event fires `not_confirmed`. This happens: U-tec sometimes accepts a command and the lock never acts on it.
- **If U-tec refuses.** For example, a lock U-tec says is offline. You get an error right away that says why, and the event fires `rejected`.
- **If U-tec doesn't answer.** The integration tries once more. If that fails too, you get an error saying the lock may still act, and Home Assistant keeps checking so you'll see what really happened.

Commands are never queued for later. If a command fails, it fails now, not as a surprise unlock ten minutes from now.

## Lock mode and Passage

Each lock has a **Lock mode** select with U-tec's three modes:

| Mode | What it does |
|---|---|
| Normal | Regular operation. |
| Passage | The lock stays unlocked. On latch models this is the only way to stop auto-lock. |
| Locked (mode 2) | U-tec's documentation names this mode but doesn't describe it. (to verify) Test it with a key at hand before relying on it. |

The select shows the mode the lock last reported. Choosing a mode always sends the command, then Home Assistant checks until the lock reports the new mode.

**Latch auto-lock.** U-tec latch locks lock themselves after a while, and that can't be turned off. If you want a door to stay open during the day, use Passage mode rather than an automation that keeps unlocking it. One command instead of dozens, and the door never relocks in between. If more than 6 commands go to one lock within 10 minutes, Home Assistant shows a repair suggesting Passage mode.

When you switch from Passage back to Normal, what the bolt does next depends on the model (to verify). If you want the door locked at closing time, add a lock action after the mode change (see the example below).

## Options

**Settings → Devices & services → U-tec Locks → Configure.**

| Option | Default | Notes |
|---|---|---|
| Polling interval | 30 s | 30 to 3,600 seconds. 30 seconds is the minimum and can't be lowered. |
| Use push notifications | On | Needs Nabu Casa or an HTTPS external URL. |
| Slow down polling while push is healthy | Off | When push is proven to be working, poll less often. Goes straight back to the normal interval the moment push misses a change. |
| Interval while push is healthy | 120 s | 60 to 1,800 seconds. Only used with the option above. |
| Confirm commands with quick checks | On | The up-to-10 checks after each of your commands. |

## Push notifications

When push is on, the integration registers a URL with U-tec: your Nabu Casa cloudhook if you have Home Assistant Cloud, otherwise your external HTTPS URL. It re-registers with a new secret once a day. Every push must carry that secret or it's rejected.

**Push status** tells you how it's going:

| Status | Meaning |
|---|---|
| Unverified | Registered, but nothing has happened yet to prove it works. |
| Healthy | Push delivered the recent changes Home Assistant saw. |
| Degraded | Push delivered some changes and missed others. |
| Unhealthy | Push missed the last changes. Polling carries on as normal. |
| Registration failed | U-tec didn't accept the registration. It's retried automatically. |
| No URL | No HTTPS URL available, so push isn't possible. |
| Disabled | Turned off in options. |

"Missed" means polling found a change that push never delivered. A quiet lock with no pushes is not counted against push.

If push stays unhealthy for a day, a repair explains what to check. Nothing speeds up when push is down: polling stays at your interval.

Two things to know:

- U-tec has no way to remove a registered URL. After you remove this integration U-tec may keep trying to send to it for a while; Home Assistant ignores those requests.
- U-tec seems to keep one push URL per account. If another integration or app registers its own URL with the same account, one of them loses push. See [Moving from the u_tec integration](#moving-from-the-u_tec-integration).

## API usage and why there's a 30-second minimum

Every install of every U-tec integration shares the same U-tec servers, and U-tec publishes no limits. Other smart-home companies have added caps or cut off access after heavy third-party use. The 30-second minimum keeps one install at about 2,900 requests a day however many locks it has, and the quick checks after your commands mean you don't need faster polling to get fast feedback.

The account device shows your numbers: total requests, the last 24 hours, and a projection for your current settings. If a number looks high, the diagnostics download shows where requests went.

## Examples

**Tell me when a lock command didn't take:**

```yaml
automation:
  - alias: "Front door: lock command didn't take"
    triggers:
      - trigger: state
        entity_id: event.front_door_command_result
    conditions:
      - condition: template
        value_template: >
          {{ trigger.to_state.attributes.event_type in ['not_confirmed', 'rejected'] }}
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: >
            Front door {{ trigger.to_state.attributes.command }}:
            {{ trigger.to_state.attributes.event_type | replace('_', ' ') }}.
```

**Shop door in Passage mode during business hours, locked at close:**

```yaml
automation:
  - alias: "Shop door: business hours"
    triggers:
      - trigger: time
        at: "08:00:00"
        id: open
      - trigger: time
        at: "17:30:00"
        id: close
    actions:
      - action: select.select_option
        target:
          entity_id: select.shop_door_lock_mode
        data:
          option: "{{ 'passage' if trigger.id == 'open' else 'normal' }}"
      - if:
          - condition: trigger
            id: close
        then:
          - wait_template: "{{ is_state('select.shop_door_lock_mode', 'normal') }}"
            timeout: "00:01:00"
          - action: lock.lock
            target:
              entity_id: lock.shop_door
```

**Warn me if a lock's status has been stale for 10 minutes:**

```yaml
automation:
  - alias: "Lock status stale"
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.front_door_status_stale
          - binary_sensor.back_door_status_stale
        to: "on"
        for: "00:10:00"
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "{{ trigger.to_state.name }}: Home Assistant hasn't heard from this lock for 10 minutes."
```

**Low battery:**

```yaml
automation:
  - alias: "Lock battery low"
    triggers:
      - trigger: state
        entity_id: binary_sensor.front_door_battery
        to: "on"
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "Front door lock battery is low."
```

## Troubleshooting

| Symptom | What to check |
|---|---|
| No locks found during setup | In the Xthings app, OpenAPI page, make sure the locks are selected. |
| Lock shows unknown and Status stale is on | U-tec isn't answering, or isn't reporting this lock. Look at **API errors** on the account device. U-tec returns occasional server errors; the integration backs off and keeps trying. |
| Lock shows unknown and Cloud connection is off | U-tec says the lock is offline. Check its Wi-Fi or bridge. Commands are still sent, but U-tec will likely refuse them. |
| State lags behind changes made at the door | Expected when push isn't working. Check **Push status**. See [Is this integration for you?](#is-this-integration-for-you) |
| Push status stays Unverified | Lock or unlock once from Home Assistant; that creates evidence. |
| Push status No URL | Set up Nabu Casa, or an HTTPS external URL under Settings → System → Network. |
| "Re-authentication required" | Follow the repair or the integration card's prompt to sign in again. |
| "U-tec is rate limiting requests" | Wait the time shown. If it keeps happening, check the request numbers on the account device and open an issue. |

To turn on debug logs: **Settings → Devices & services → U-tec Locks → Enable debug logging**, reproduce the problem, then disable it to download the log. Logs never contain tokens, secrets, or push bodies.

## Known limitations

- State changes made at the lock or in the U-tec app can take up to the polling interval (30 seconds by default) to show up when push isn't working, and longer during U-tec outages.
- U-tec reports battery in five steps, not a percentage.
- U-tec's API has no auto-lock setting. Use Passage mode to keep a latch open.
- What "Locked (mode 2)" does is not documented by U-tec (to verify).
- Lock users and PIN codes can't be managed from this integration.
- No local control. Everything goes through U-tec's cloud.
- U-tec's API exposes no Wi-Fi bridge or other non-lock devices to this integration, and lights, switches and plugs are deliberately not supported.

## Supported devices

Any U-tec / Ultraloq lock that appears in the U-tec OpenAPI with category `SmartLock` (handle types `utec-lock` and `utec-lock-sensor`). Tested so far: (list filled in after the soak).

## Diagnostics and privacy

**Settings → Devices & services → U-tec Locks → three dots → Download diagnostics.** Tokens, secrets, your client ID, push URLs, your name and user id, and serial numbers are removed. Lock ids are replaced with short codes.

The integration talks only to U-tec. It sends no data anywhere else.

## Moving from the u_tec integration

This is a separate integration with its own domain, not an update to `u_tec` (Uhome-HA). **You cannot run both at once:** if `u_tec` is still loaded, U-tec Locks will refuse to set up (they would double your API use and take push from each other).

1. Note which automations, scripts and dashboards use your `u_tec` lock entities.
2. Disable (or delete) the `u_tec` integration entry.
3. Set up U-tec Locks. You can reuse your Client ID and Secret.
4. Rename the new entities to the old entity ids if you want your automations to keep working unchanged.
5. Remove `u_tec` once you're happy.

## Removing the integration

1. **Settings → Devices & services → U-tec Locks → three dots → Delete.**
2. Remove it from HACS (or delete `custom_components/utec_locks`) and restart Home Assistant.
3. Optional: in the Xthings app, turn off OpenAPI or reset the Client Secret.
4. Optional: delete the credential under **Settings → Application credentials**.

## FAQ

**Can I poll faster than 30 seconds?** No. It's a fixed floor, for the reasons in [API usage](#api-usage-and-why-theres-a-30-second-minimum). Commands you send from Home Assistant are still confirmed within seconds.

**Why doesn't the lock show "locked" right after I press Lock?** Because it isn't locked yet. U-tec only says it received the command. The lock shows "locking" until U-tec reports that the lock actually locked.

**Why unknown instead of unavailable?** Home Assistant silently ignores commands sent to unavailable entities. A lock you can't command is worse than a lock whose state is uncertain, so it stays available and says unknown.

**Does this work without Nabu Casa?** Yes. Without Nabu Casa or an HTTPS external URL there's no push, and everything runs on polling.

## Contributing

Issues and pull requests from outside contributors are welcome. Rob remains the sole code owner for now (no co-owners). Changes that add API requests need a note on how many and why. Tests must pass with at least 95% coverage.

## License

MIT. U-tec, Ultraloq and Xthings are trademarks of their owners and are used here only to describe what this integration works with.
