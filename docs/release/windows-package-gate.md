# Windows package gate

The Windows package builder is a release gate. It must run in a complete
PowerShell 7 runtime on a Windows runner; a partial `pwsh` bundle is not an
acceptable substitute because package assembly and hash verification depend on
the standard filesystem cmdlets.

`scripts/build-windows-package.ps1` performs a preflight before it resolves the
source directory or creates a staging directory. It verifies that
`Microsoft.PowerShell.Management` is discoverable and loadable, then checks
that `Resolve-Path`, `Test-Path`, `Copy-Item`, and `Get-FileHash` are available
as cmdlets. A failed preflight exits with status `78` and a diagnostic whose
prefix is:

```text
NOYRA-WINDOWS-PACKAGE-PREREQUISITE[...]:
```

The diagnostic identifies either the missing module (`PS-MANAGEMENT-MODULE`)
or cmdlet (`PS-CMDLET`) and tells the operator to use a complete PowerShell 7
runtime. This prevents a missing module from being reported as a package input,
hash, or signing failure.

The supported CI gate is the `windows-2022` matrix job in
`.github/workflows/ci.yml`. It must run both package lifecycle tests and the
hash-tampering rejection test. A local or embedded runtime that cannot load
the Management module remains an external gate failure; it must not be
converted into a passing release result.


## Launcher and publisher contract

A package build is not complete without an explicit `-LauncherPath` pointing to
the signed Noyra launcher executable. The builder writes the launcher as the
manifest-declared `noyra.exe` entry; it no longer silently creates a source-only
package that the lifecycle can install but never start. For strict signing, pass one or more approved publisher certificate thumbprints
with `-TrustedPublisherThumbprints` and `-RequireSignature`, plus
`-ManifestSignerThumbprint` when building. The builder emits a detached CMS
signature at `noyra-package.json.p7s`; the lifecycle verifies that signature,
the exact manifest bytes, the manifest inventory, the launcher, and every
packaged executable against the external publisher allowlist. It never trusts a
thumbprint or public key that is introduced only by the package itself.

After the first strict install, the lifecycle retains the explicitly supplied
allowlist in `trusted-publishers.json` under the install root. Later `start`,
`rollback`, and strict `upgrade` operations may reuse that policy when the
operator does not pass the parameter again. A missing or malformed policy is a
hard failure; it is not regenerated from package metadata.

The thumbprint allowlist is an operator/release policy input and must be
reviewed and rotated with the publisher certificate. The manifest signer must
be one of the allowlisted certificates and must be available with its private
key to the build process. Certificate rotation therefore requires an explicit
trust-policy update and a clean-VM install/upgrade/rollback check. The Windows
CMS signature complements, rather than replaces, the release pipeline's
checksum signature and clean-VM evidence gate.


## Recovery records and certificate rotation

The lifecycle normalizes omitted, null and empty publisher parameters before
loading saved policy. One or more explicit pins remain a per-call override;
only a successful strict install persists that allowlist.

Each new installation writes `verification-policy.<version>.json` beside the
version pointers, outside the package inventory. This operator-controlled
record stores the signature requirement that passed verification before the
current pointer is published. Rollback reads that requirement and completely
verifies the previous package against the effective external publisher policy;
it does not require the damaged current package to pass verification. Invalid
records fail closed. A legacy install with no record conservatively requires
signed recovery when a publisher policy is configured. Legacy unsigned
installations without publisher policy can recover their verified development
packages. A new development install records its own unsigned requirement;
historical pins do not silently create a permanent global strict-mode switch.
The per-call `-RequireSignature` switch still enforces signed recovery.

Package publisher metadata describes the package; it cannot grant trust. Every
actual manifest and executable signer must be in both the signed metadata and
the external allowlist. Extending external policy from A to A+B permits rollback
to a valid A package. Removing A rejects actual A signatures. An unused A entry
in metadata does not prevent verification of a package actually signed by B.

The native regression fixture uses in-memory certificates for detached CMS and
a signed Windows system binary as an unexecuted launcher fixture. It does not
import certificates, change the system certificate store, start an executable,
or replace the official publisher and clean-VM release gates.
