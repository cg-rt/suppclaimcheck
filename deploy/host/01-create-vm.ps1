#Requires -RunAsAdministrator
<#
.SYNOPSIS
Creates the production VM scc-prod in Hyper-V (C0.4, D-007).

.DESCRIPTION
Generation 2, 4 vCPU, 10 GB static memory, a 120 GB dynamic disk, the Default Switch, Secure Boot with the
Microsoft UEFI CA template (needed for Linux), the Ubuntu Server ISO as the first boot device, automatic start
with Windows and a clean guest shutdown when Windows stops. Automatic checkpoints are off.
Stops without changing anything if a VM with the same name exists.

.EXAMPLE
.\deploy\host\01-create-vm.ps1 -IsoPath D:\VMs\iso\ubuntu-26.04.1-live-server-amd64.iso
#>
param(
    [Parameter(Mandatory = $true)] [string] $IsoPath,
    [string] $Name = 'scc-prod',
    [string] $VmRoot = 'D:\VMs',
    [int] $Cpus = 4,
    [long] $MemoryBytes = 10GB,
    [long] $DiskBytes = 120GB,
    [string] $Switch = 'Default Switch'
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $IsoPath)) { throw "ISO not found: $IsoPath" }
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) { throw "A VM named $Name already exists. Nothing changed." }

$disk = Join-Path $VmRoot "$Name\$Name.vhdx"
New-VM -Name $Name -Generation 2 -MemoryStartupBytes $MemoryBytes -Path $VmRoot `
    -NewVHDPath $disk -NewVHDSizeBytes $DiskBytes -SwitchName $Switch | Out-Null
Set-VM -Name $Name -ProcessorCount $Cpus -StaticMemory -AutomaticStartAction Start -AutomaticStartDelay 30 `
    -AutomaticStopAction ShutDown -AutomaticCheckpointsEnabled $false
Set-VMFirmware -VMName $Name -SecureBootTemplate MicrosoftUEFICertificateAuthority
Add-VMDvdDrive -VMName $Name -Path $IsoPath
Set-VMFirmware -VMName $Name -FirstBootDevice (Get-VMDvdDrive -VMName $Name)

Get-VM $Name | Format-List Name, Generation, State, ProcessorCount, MemoryStartup, DynamicMemoryEnabled,
    AutomaticStartAction, AutomaticStartDelay, AutomaticStopAction, AutomaticCheckpointsEnabled, Path
Get-VMFirmware $Name | Format-List SecureBoot, SecureBootTemplate
Get-VMHardDiskDrive $Name | Format-List Path
Write-Host "Next: Start-VM $Name; vmconnect.exe localhost $Name   (install steps in deploy/host/README.md)"
