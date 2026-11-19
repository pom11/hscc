kind: Verified
task: t_c1a1bf8e

- **The cluster widgets’ large/XL tables, the Cluster/Monitor Lock Screen
  accessories and the Monitor Live Activity are on current `main`.** #409’s
  large/XL table work had already landed via #424 (`d385a3ca`), so it was closed
  as superseded; #415 and #423 were rebased onto `main` preserving both sides’
  newer widget work, and merged after the iOS/macOS compile, decode, widget and
  address gates passed. No product behaviour changed beyond the rebases.
