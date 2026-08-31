// Bump this on every commit that touches the frontend. It's shown as a
// small footer line on the page purely so you can glance at a running
// deploy and confirm the server actually rebuilt the version you expect,
// instead of quietly serving a stale container.
//
// Convention: v1.<total commit count on the repo>, e.g. `git rev-list
// --count HEAD` == 13 -> "v1.13".
export const APP_VERSION = "v1.23";
