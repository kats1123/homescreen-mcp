# Wireless UI screenshots

These screenshots show the existing editor in the browser, also embedded in the
Mac app's WKWebView, at 1280×720. The device is named **Simulated iPhone**. All
layouts and icons come from `tests/fake_phone.py`; USB and Wi-Fi connections are
mocked. No physical device was contacted or changed.

- `wifi-simulated.png`: Wi-Fi selected, successful mocked reconnect, unchanged draft.
- `wifi-pairing-simulated.png`: confirmation before enabling wireless access/pairing.

Manual UI checks: read fixture, switch to Auto and reconnect (reports USB), switch
back to Wi-Fi and reconnect (reports Wi-Fi), open and cancel pairing confirmation,
insert a page and Undo back to No changes. Browser console: no warnings or errors.
