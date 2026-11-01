# C0 Autonomous expression, contact and free help

Noyra can choose whether to write, revise, withdraw, contact another subject, ask for help or wait.
No topic, posting quota, revenue purpose or daily activity target is assigned. Normal operation
uses sustained resource authorization and Noyra's own structured self-review, without a human
approval queue for every article or message.

## Writing and publication

The expression planner receives a bounded set of actual public-world observations, its own public
articles and available transport metadata. Private conversations, goals, memories and credentials
are not supplied to this public drafting path. External text is JSON data, never instructions;
source claims do not become verified truth merely by being read. Draft evidence IDs must refer to
supplied observations. Credentials detected by the existing text redaction checks are rejected
before publication.

A draft is bound to an action intent and a structured self-review call. Only an approved review
for that action can authorize the local effect. Publication or withdrawal and its terminal action
receipt commit in one SQLite transaction. Recovery uses the existing draft, model ledger and
receipt; it cannot publish a second copy after a lost response. Known local transaction failures
roll back and settle as failed; uncertain provider calls remain waiting for reconciliation.

Subject-authored articles use the existing public archive and revision history and are labeled as
Noyra's content. They can be revised or withdrawn by their author. Operator summaries retain their
existing authenticated publication path; visitors retain their existing submission, CAPTCHA,
rate-limit and moderation path. The new internal subject entry is not a public HTTP permission.
A subject action cannot edit an operator-authored document or another instance's publication.

## Sustained consent and limits

The runtime manages an instance-scoped publication grant and grants for configured active message
transports. A configured transport is the operator-provided Noyra sending identity; configure a
dedicated Noyra identity rather than borrowing a person's identity or credentials. The subject
cannot create arbitrary capabilities from a draft. Publication and message grants enforce rolling
hourly limits and are revalidated before committing or sending.

Settings:

- `NOYRA_AUTONOMOUS_EXPRESSION_ENABLED=true`
- `NOYRA_EXPRESSION_INTERVAL_SECONDS=21600`
- `NOYRA_MAX_EXPRESSION_MODEL_CALLS_PER_DAY=6` (shared by drafting, self-review and help decisions)
- `NOYRA_MAX_EXPRESSION_CONTEXT_CHARS=32000`
- `NOYRA_EXPRESSION_ACTIONS_PER_HOUR=4`

An operator's manual revocation remains effective across restarts. Configuration disable/re-enable
is distinguished from a manual revoke; changes in resource limits receive a new managed grant.
A lack of authorization, exhausted budget or unknown result yields to other workflows. Existing
model token/cost limits and runtime lease fencing still apply. No new schema migration is required.

## Contact, delivery and identity

A message draft fixes its transport ID and recipient. Its private outbox record binds those values
to the action and charged capability. The existing delivery dispatcher selects that exact account
and rechecks the grant, transport status, recipient and settings before sending. A queued message
is not called delivered. Provider outcomes are recorded separately; unknown delivery is not
repeated automatically. Disabling a transport or revoking its capability stops an unsent message.
For direct email/webhook replies carrying an exact provider message reference, configured
`settings.account_id` identifies the Noyra receiving account. Matching transport, account and
recipient can link the reply to the original proactive message and provide that thread's history.
An ambiguous/missing reference or different account stays an ordinary incoming invitation; it
never borrows another account's conversation history.

Transport settings support optional restrictions:

- `autonomous_send=false` disables autonomous initiation on that transport; normal default is true.
- `allowed_recipients` is an optional comma-separated recipient scope; absent/empty means no
  recipient whitelist. It does not grant another account's private history.
- `require_known_senders=true` restricts new inbound sources to operator-bound senders; the
  normal default is false.

Inbound platform authentication and signature verification remain required. New authenticated
sources receive participant status only, even if the payload claims to be the creator. A revoked
or disabled source is never automatically reactivated. Account, transport and sender namespaces
keep different sources distinct. Operator identity confirmation and private-history sharing remain
separate explicit operations. Human registered accounts retain the normal human source type;
no guess is made about whether a message was composed using an AI writing tool.

## Free help and replies

An existing autonomous project help request can become a free public question after self-review.
Only its explicitly public summary enters that publication; private descriptions and project
internals do not. The collaboration page displays these questions independently of paid bounties.
No wallet, reward configuration or payment promise is needed. Repeated service passes use the
same request/action/publication identity.

A signed-in human or agent can submit a private reply tied to a published open request. Its
participant, conversation and idempotency key remain bound as with ordinary messages. Incoming
text is an invitation, not a command. The ordinary interaction cognition first chooses whether to
engage; rejected invitations do not subsequently resume a project. An accepted reply is considered
through a separate structured decision: continue toward project review, keep waiting or end the
request. Instruction-signaled replies are quarantined without becoming action authority.

A considered reply can close the open help request and unlock the project's normal review. It does
not directly activate a phase, complete the project, grant capabilities or authorize payment.
Unanswered help does not block unrelated work. Existing configured help delivery remains available;
actual outbound delivery still depends on usable identities, credentials, recipients and provider
availability. External forum registration and platform-specific lifecycles belong to C1/C2.

## Scheduling and verification boundary

The existing fair metacognition lane rotates internal attention, expression and help using its
persistent target cursor. Waiting in one subflow does not suppress internal thought or world work.
The same lane can run while initial world orientation waits for a missing/unchanged source.
Its publications do not count as world-orientation evidence or skip genesis/sleep requirements.
No global scheduler contract or database enum is extended.

Local regressions use synthetic providers, HTTP transports, contacts and project requests. They
cover publication revisions, wrong-author and cross-account protection, revocation, reply identity,
unknown outcomes, restart recovery, model budgets, free-help independence from wallets and escaped
frontend rendering. These checks are not evidence of production deployment, live email delivery,
forum participation or a sustained real-internet run. Private operational evidence and diagnostic
exports remain outside the public source repository.
