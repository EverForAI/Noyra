# Model resource endpoint policy

New operator-provided cognitive resources require public HTTPS endpoints. Literal
IPv4 and IPv6 addresses use `core.http.is_public_address`, the same public-unicast
policy used before the real HTTP connection. Translation ranges, multicast,
reserved addresses and scoped IPv6 literals are rejected at configuration time.
Saving a hostname does not perform DNS or make a provider request. Resolution,
address validation and connection pinning remain the transport's responsibility.

Stored resource hashes and revision history keep their original validation
contract. An older resource containing a then-accepted special address remains
readable, disableable and revocable; an upgrade does not rewrite its URL, hashes
or history or make every resource listing fail. This is compatibility for stored
evidence, not permission to connect: the existing public transport still rejects
that destination before opening a socket. Operators can disable/revoke the old
resource and create a replacement with a supported endpoint. New configuration
also rechecks the URL at the store boundary, including copied pydantic inputs.

The separate `OpenAICompatibleSettings.allow_local_endpoint` opt-in continues to
support an explicitly configured local provider. It does not grant operator
cognitive resource pools access to HTTP or loopback URLs. No schema migration or
historical resource rewrite accompanies this policy alignment.
