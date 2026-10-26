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

If installation stops after promoting `versions/<version>` but before publishing
its receipt, trust policy, or version pointer, repair the external filesystem
fault and run the same install/upgrade command again. The lifecycle verifies both
the supplied package and the existing version, including signatures, publisher
policy, full inventory, and hashes, then requires identical manifest bytes before
finishing publication. It never replaces or deletes that existing version.
A successful retry of the selected version preserves the previous-version
pointer. Damaged or different same-version contents require operator review;
they are never silently overwritten. Installation and rollback share the process
lifecycle lock for the configured data root.

## R03 authoritative lifecycle state and recovery

The Windows lifecycle now commits the selected version, rollback selection,
verification policy, trust pins and generation through one `r03-state.json`
authority. `current.txt`, `previous.txt` and `trusted-publishers.json` remain
compatibility projections only; supported lifecycle operations do not derive
selection or trust from them after protocol adoption. The helper
`deploy/windows/noyra-r03-recovery.ps1` is packaged and signed by the package
manifest together with the lifecycle entry.

On a legacy installation, the new lifecycle validates the legacy pointer paths
and referenced version trees before recording an authority state. A new install
or upgrade verifies and stages its candidate before publishing acceptance. The
new trust policy becomes effective when the durable `prepared` authority record
is published. If publication has not succeeded, the previous trust policy and
selection remain authoritative. After it succeeds, recovery only moves forward
or remains blocked; it never re-adds a signer removed by that intent. Start,
rollback and another install/upgrade first recover a pending transaction. Stop
remains available while recovery is blocked; uninstall requires a ready state
and retains `DataRoot`.

Inspect state with the lifecycle `-Action status`; the JSON includes protocol
version, generation, recovery phase, target operation/version and whether
recovery is required. To retry a recoverable transaction, run the same supported
PowerShell 7 lifecycle script with `-Action recover`, `-InstallRoot` and
`-DataRoot`. If the authority is missing after the protocol marker was written,
or the authority is corrupt or bound to another installation, the lifecycle
fails closed and will not reconstruct trust from compatibility files. Do not
delete the marker or authority to force legacy fallback; preserve the tree and
use an operator-reviewed repair procedure.

All supported start/stop/upgrade/rollback/status/recover actions must use the
same current lifecycle implementation and data root. Historical package copies
of older lifecycle scripts do not understand R03 and are not supported control
entries after adoption. An administrator who deliberately runs stale code can
modify local installation files and is outside the filesystem protocol's
protection boundary; deployments that must defend against a hostile local
administrator require a separate OS-enforced entry/version gate. The SHA-256
inside the authority envelope detects accidental corruption; it is not an
administrator-resistant signature or external anti-rollback counter.

The native regression suite exercises signed A-to-B rotation, actual signer
revocation, bootstrap, rollback, per-file sharing failures and recovery retry
without executing packaged launchers or importing test certificates. It does
not prove sudden power-loss durability or protect against malicious local
administrators; those remain separate deployment acceptance boundaries.

## Retained data-root launch contract

The supported lifecycle passes an absolute `DataRoot` in a child-only environment; it never changes the caller's environment. The standard service entry verifies that data root before creating directories or opening SQLite. Source/Linux/container launches without a managed contract keep their existing configuration.

An existing regular nonempty `noyra.sqlite3` is opened using the existing identity, ownership and integrity gates. A missing or empty database is not silently replaced. A deliberately new instance requires `-Action start -InitializeDataRoot`, and the directory must have no subject data. This switch is not a recovery or data-migration operation.

New process records use `noyra-process/v2` and include the bound data root. Valid v1 records remain usable for stopping the matching process; an already running v1 process must be stopped and its actual data location reviewed before a new managed start.

The entry scans the installation tree before adoption/version changes and before uninstall. Legacy `.runtime` or `noyra.sqlite3*` entries block these operations; links or unreadable trees fail closed. `stop` and `status` remain available. Status reports `data_root_status` and `legacy_data_paths`; uninstall rechecks after stopping before removal. See the [legacy-data migration runbook](../deployment/windows-data-root-migration.md); the lifecycle does not automatically migrate or merge old subjects.
