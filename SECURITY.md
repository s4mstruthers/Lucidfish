# Security policy

## Supported versions

Only the latest release on the [releases page](https://github.com/s4mstruthers/Lucidfish/releases)
receives fixes. The desktop app updates itself to it.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. You should hear back within seven days.

Problems worth reporting include, for example:

- an API key reaching a log, an exported file, a shared profile page or a request it should not;
- the local web server (`lucidfish web`, bound to `127.0.0.1` by default) being reachable from
  elsewhere, or serving something outside its own files;
- a crafted PGN or a shared profile page that can run code or read files on the viewer's computer;
- the desktop updater accepting an update that is not signed with the project's key.

## What Lucidfish does with secrets

API keys are stored in the operating system's keychain (through the `keyring` library), in the
environment, or for the session only; they are never written to the database or to a shared page.
Settings and games live in the per-user data folder and never leave the computer unless you choose
a cloud coach, in which case the positions and evidence for a game are sent to that provider under
your own key. See *Privacy and your data* in the README.
