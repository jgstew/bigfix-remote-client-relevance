# BigFix SaaS fast query captures

Response bodies from BigFix SaaS, captured 2026-10-27 through
bigfix-remote-session-relevance 1.3.0's `SaasBackend` (one Windows agent,
client 11.0.5.204). Redacted: computer ids are `11`, the computer name is
`pc11`; nothing else in these bodies identified the deployment.

- `create_ok.json`: `POST /core/v2/clientquery/create`, accepted.
- `create_500.json`: the same call, HTTP 500. Byte-for-byte the same body for
  two different causes: a computer id the deployment does not have (REST
  accepts one silently; SaaS refuses it) and client relevance containing a
  control character. The body never says which, so a failed submit can only
  be reported, never diagnosed or safely resent.
- `listresults_answered.json`: `POST /bfproxy/v1/clientquery/listresults`
  once the agent answered.

Response headers carried no rate-limit information (`content-type` only).
