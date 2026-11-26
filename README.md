# valorant party joiner

A small Windows app that retries a party code using your running Riot Client session. It stops once your membership is confirmed.

## Running it

Requires Windows and Python 3.10 or newer with tkinter. No additional packages are needed.

1. Download or clone this repository.
2. Open VALORANT and wait for the main menu.
3. Double-click `Start.bat`, enter the party code and press **Start retrying**.

You can also run it from a terminal:

```powershell
py -3 valorant_party_joiner.py
```

If Python is missing, install it from [python.org](https://www.python.org/downloads/windows/) with the Python launcher and Tcl/Tk support enabled.

## Controls

- **Target interval:** 0.1 to 300 seconds. Press **Apply** or Enter to change it while running.
- **Region:** leave it on Auto unless detection fails. This is your account's game region.
- **Show:** reveals the party code in the input box.
- **Stop:** cancels further attempts. Escape also works while this window has focus.

## Timing and responses

The interval is measured between request starts. Only one join request is sent at a time, so a response that takes longer than the target also delays the next attempt. The window displays the actual gap and response time.

HTTP connections are reused. While the party remains full, session and party checks are cached for up to two seconds. Uncertain results force a fresh check before another join.

`403 / PARTY_FULL` is retried. Other 403 responses stop the run. Server cooldowns and temporary failures can increase the interval; lowering the setting does not bypass them.

After an accepted request, the app checks your party membership before reporting success. It does not automatically restart if you later leave or get removed. A request already sent may finish after you press Stop.

Full-party rejections appear in this app's log. They do not trigger VALORANT's own error dialog.

## Tests

```powershell
py -3 -m unittest -v
```

The tests use simulated responses and make no requests to Riot. They cover retry timing, cooldowns, authentication changes, cancellation, connection reuse and membership confirmation. A successful join and automatic stop have also been confirmed in the running game.

## Notes

The app reads the local Riot Client lockfile and uses its existing session. It does not save party codes, tokens or activity logs. It does not inject code or read game memory.

This uses an unofficial client interface. It is not Riot-approved, and compatibility or account safety cannot be guaranteed. See [Riot's third-party application policy](https://support.riotgames.com/en-us/riot/events/third-party-applications).

Endpoint references: [party codes](https://valapidocs.techchrism.me/endpoint/party-join-by-code), [party membership](https://valapidocs.techchrism.me/endpoint/party), [local authentication](https://valapidocs.techchrism.me/endpoint/entitlements-token).
