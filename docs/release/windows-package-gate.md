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
