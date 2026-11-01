param(
    [Parameter(Mandatory=$true)][string]$VerifiedRollbackProfile,
    [Parameter(Mandatory=$true)][string]$ApprovedPlan,
    [Parameter(Mandatory=$true)][ValidateSet('scanner','listener')][string]$Role,
    [Parameter(Mandatory=$true)][string]$PacketRoot
)
$ErrorActionPreference = 'Stop'
# Quiescence and approved checkout of compatible rollback 911147c must already
# have been verified by the operator. This wrapper never changes Git or SQLite.
& (Join-Path $PSScriptRoot 'start_runtime.ps1') -Role $Role -Release rollback `
  -ProposedProfile $VerifiedRollbackProfile -ApprovedPlan $ApprovedPlan -PacketRoot $PacketRoot
if ($LASTEXITCODE -ne 0) { throw 'Compatible rollback launch failed; preserve current state and inspect' }
