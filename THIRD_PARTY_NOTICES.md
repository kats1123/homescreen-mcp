# Wireless implementation references

The userspace RemotePairing provider adapter in `homescreen/transport.py` is
adapted from [ios-homescreen's transport.py](https://github.com/YakupEmreYerli/ios-homescreen/blob/97649547dbc44295e451c6bf130a87404481e10c/src/ios_homescreen/transport.py).
[Rejig](https://github.com/rejig-app/rejig) uses that engine; its
[wifi-reconnect](https://github.com/rejig-app/rejig/blob/a0b6a97d132e151bf8a53e0cb3d016f8caf15ab0/patches/wifi-reconnect.patch)
and [device-identity](https://github.com/rejig-app/rejig/blob/a0b6a97d132e151bf8a53e0cb3d016f8caf15ab0/patches/device-identity.patch)
patches informed same-device reconnection and pre-write identity checks here.
This implementation does not automatically retry writes or roll back layouts.

MIT License

Copyright (c) 2026 Yakup Emre Yerli
Copyright (c) 2026 keyclipse

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Existing runtime dependency

[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) by doronz88 and
contributors is an existing dependency licensed under GPL-3.0-or-later. Its
[license](https://github.com/doronz88/pymobiledevice3/blob/master/LICENSE) and
those of its dependencies continue to apply when distributing a bundled engine.
Apple's device protocols are implemented by that library, not copied here.
