# Batchability spike scores

## Actual outcomes per host

| host | qna | total | one | error | env-error | empty | plural | split |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| macOS 15.7 x86_64 (SSH, installed agent) | 11.0.7.61 | 545 | 291 | 139 | 0 | 85 | 14 | 16 |
| Rocky Linux 9.3 arm64 (container) | 11.0.7.61 | 545 | 176 | 143 | 107 | 102 | 6 | 11 |
| Ubuntu 26.04 arm64 (container) | 11.0.7.61 | 545 | 198 | 135 | 90 | 105 | 6 | 11 |

## Precision of a "safe to batch" verdict

Container environment errors (no SMBIOS, Rocky's RPM symlink) are left out of every count below; the raw counts are in the table above.

| predictor | half | host | predicted safe | actually one | precision | recall |
|---|---|---|---:|---:|---:|---:|
| classifier (platform-agnostic) | dev | macOS 15.7 x86_64 (SSH, installed agent) | 146 | 120 | 82.2% | 81.6% |
| classifier (platform-agnostic) | dev | Rocky Linux 9.3 arm64 (container) | 101 | 73 | 72.3% | 83.0% |
| classifier (platform-agnostic) | dev | Ubuntu 26.04 arm64 (container) | 104 | 80 | 76.9% | 82.5% |
| classifier (platform-agnostic) | dev | all hosts | 351 | 273 | 77.8% | 82.2% |
| classifier (platform-agnostic) | holdout | macOS 15.7 x86_64 (SSH, installed agent) | 160 | 126 | 78.8% | 87.5% |
| classifier (platform-agnostic) | holdout | Rocky Linux 9.3 arm64 (container) | 113 | 79 | 69.9% | 89.8% |
| classifier (platform-agnostic) | holdout | Ubuntu 26.04 arm64 (container) | 118 | 89 | 75.4% | 88.1% |
| classifier (platform-agnostic) | holdout | all hosts | 391 | 294 | 75.2% | 88.3% |
| classifier (platform-agnostic) | all | macOS 15.7 x86_64 (SSH, installed agent) | 306 | 246 | 80.4% | 84.5% |
| classifier (platform-agnostic) | all | Rocky Linux 9.3 arm64 (container) | 214 | 152 | 71.0% | 86.4% |
| classifier (platform-agnostic) | all | Ubuntu 26.04 arm64 (container) | 222 | 169 | 76.1% | 85.4% |
| classifier (platform-agnostic) | all | all hosts | 742 | 567 | 76.4% | 85.3% |
| classifier (platform known) | dev | macOS 15.7 x86_64 (SSH, installed agent) | 122 | 119 | 97.5% | 81.0% |
| classifier (platform known) | dev | Rocky Linux 9.3 arm64 (container) | 74 | 72 | 97.3% | 81.8% |
| classifier (platform known) | dev | Ubuntu 26.04 arm64 (container) | 81 | 79 | 97.5% | 81.4% |
| classifier (platform known) | dev | all hosts | 277 | 270 | 97.5% | 81.3% |
| classifier (platform known) | holdout | macOS 15.7 x86_64 (SSH, installed agent) | 130 | 125 | 96.2% | 86.8% |
| classifier (platform known) | holdout | Rocky Linux 9.3 arm64 (container) | 80 | 78 | 97.5% | 88.6% |
| classifier (platform known) | holdout | Ubuntu 26.04 arm64 (container) | 91 | 88 | 96.7% | 87.1% |
| classifier (platform known) | holdout | all hosts | 301 | 291 | 96.7% | 87.4% |
| classifier (platform known) | all | macOS 15.7 x86_64 (SSH, installed agent) | 252 | 244 | 96.8% | 83.8% |
| classifier (platform known) | all | Rocky Linux 9.3 arm64 (container) | 154 | 150 | 97.4% | 85.2% |
| classifier (platform known) | all | Ubuntu 26.04 arm64 (container) | 172 | 167 | 97.1% | 84.3% |
| classifier (platform known) | all | all hosts | 578 | 561 | 97.1% | 84.4% |
| plurality == SINGULAR only | dev | macOS 15.7 x86_64 (SSH, installed agent) | 193 | 140 | 72.5% | 95.2% |
| plurality == SINGULAR only | dev | Rocky Linux 9.3 arm64 (container) | 143 | 84 | 58.7% | 95.5% |
| plurality == SINGULAR only | dev | Ubuntu 26.04 arm64 (container) | 149 | 94 | 63.1% | 96.9% |
| plurality == SINGULAR only | dev | all hosts | 485 | 318 | 65.6% | 95.8% |
| plurality == SINGULAR only | holdout | macOS 15.7 x86_64 (SSH, installed agent) | 193 | 135 | 69.9% | 93.8% |
| plurality == SINGULAR only | holdout | Rocky Linux 9.3 arm64 (container) | 143 | 85 | 59.4% | 96.6% |
| plurality == SINGULAR only | holdout | Ubuntu 26.04 arm64 (container) | 151 | 98 | 64.9% | 97.0% |
| plurality == SINGULAR only | holdout | all hosts | 487 | 318 | 65.3% | 95.5% |
| plurality == SINGULAR only | all | macOS 15.7 x86_64 (SSH, installed agent) | 386 | 275 | 71.2% | 94.5% |
| plurality == SINGULAR only | all | Rocky Linux 9.3 arm64 (container) | 286 | 169 | 59.1% | 96.0% |
| plurality == SINGULAR only | all | Ubuntu 26.04 arm64 (container) | 300 | 192 | 64.0% | 97.0% |
| plurality == SINGULAR only | all | all hosts | 972 | 636 | 65.4% | 95.6% |

## Classifier verdicts against actual outcomes (all hosts)

| verdict (platform known) | one | error | empty | plural | split |
|---|---:|---:|---:|---:|---:|
| safe | 561 | 17 | 0 | 0 | 0 |
| split_risk | 0 | 0 | 0 | 0 | 30 |
| plural_risk | 25 | 27 | 243 | 21 | 4 |
| unknown | 1 | 8 | 0 | 0 | 0 |
| error_risk | 78 | 365 | 49 | 5 | 4 |

## False "safe": classifier (platform-agnostic) (175)

- macOS 15.7 x86_64 (SSH, installed agent): error `r0045` `exists key "HKLM\SOFTWARE" of native registry` The operator "native registry" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0083` `exists unique values whose(it starts with "Hyper-V") of ( ( if ( exists (it as string) whose(it contains "<dmi>") of pro` The operator "version strings" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0115` `average duration of evaluationcycle of client` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0147` `( exists lengths whose(it > 1) of (it as trimmed string) of values of settings "_BESServer_Database_Server" of client ) ` The operator "native registry" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0162` `exists cloud providers` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0179` `exists keys whose(value "DisplayName" of it as string as trimmed string starts with "Dell Command | Update" AND value "D` The operator "keys" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0218` `exists wmi "root/cimv2/mdm/dmmap"` The operator "wmi" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0219` `exists selects "InstanceID from MDM_RemoteFind" of wmis "root/cimv2/mdm/dmmap"` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0220` `(exists selects "InstanceID from MDM_RemoteFind" of wmis "root/cimv2/mdm/dmmap") as string` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0221` `(concatenation ", " of (it as string) of integer values of selects "DesiredAccuracy from MDM_RemoteFind" of wmis "root/c` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0222` `(concatenation ", " of (it as string) of integer values of selects "MaximumAge from MDM_RemoteFind" of wmis "root/cimv2/` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0223` `(concatenation ", " of (it as string) of integer values of selects "Timeout from MDM_RemoteFind" of wmis "root/cimv2/mdm` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0224` `(exists selects "InstanceID from MDM_RemoteFind_Location01" of wmis "root/cimv2/mdm/dmmap") as string | "N/A"` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0225` `(concatenation ", " of (it as string) of string values of selects "Latitude from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0226` `(concatenation ", " of (it as string) of string values of selects "Longitude from MDM_RemoteFind_Location01" of wmis "ro` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0227` `(concatenation ", " of (it as string) of string values of selects "Altitude from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0228` `(concatenation ", " of (it as string) of string values of selects "Accuracy from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0229` `(concatenation ", " of (it as string) of string values of selects "AltitudeAccuracy from MDM_RemoteFind_Location01" of w` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0230` `(concatenation ", " of (it as string) of string values of selects "Age from MDM_RemoteFind_Location01" of wmis "root/cim` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0245` `exists keys "HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" of (x` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0304` `exists string values of selects "DeviceHardwareData from MDM_DevDetail_Ext01 where InstanceID='Ext' AND ParentID='./DevD` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0344` `exists debianpackages` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0345` `not exists packages "docker-ce" of debianpackages` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0346` `(((unix of operating system) AND (exists debianpackages)) AND (not exists packages "docker-ce" of debianpackages)) AND (` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0347` `((unix of operating system) AND (exists debianpackages)) AND (4 != number of (packages "apt-transport-https" of it; pack` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0348` `((((unix of operating system) AND (exists packages "apt" of debianpackages)) AND (exists packages "curl" of debianpackag` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0349` `((((unix of operating system) AND (exists architectures whose(it = "x86_64") of operating systems)) AND (exists packages` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0350` `(((unix of operating system) AND (exists packages "apt" of debianpackages)) AND (exists lines whose(it does not start wi` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0351` `exists dns domainnames of local computers of active directories` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0352` `exists (it as trimmed string) whose(it != "") of substrings separated by "," of (it as string as lowercase) of values "S` The operator "keys" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0353` `not exists (it as trimmed string) whose(it contains unique value of dns domainnames of local computers of active directo` The operator "keys" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0355` `exists packages "apt" of debianpackages` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0371` `not exists lines whose(it contains "@type %25*") of files "cat.bat" of windows folders` The operator "windows folders" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0372` `not exists lines whose(it contains "@dir %25*") of files "ls.bat" of windows folders` The operator "windows folders" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0373` `not exists values "{CA8A9780-280D-11CF-A24D-444553540000}" whose(it as string as integer = 0) of keys "HKEY_LOCAL_MACHIN` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0374` `exists keys whose( exists values "DisplayName" whose(it as string as lowercase contains "Acrobat Reader" as lowercase) o` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0375` `/* I think this is only needed if Hyper-V is in use */ exists keys "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Vi` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0376` `not exists values "MinVmVersionForCpuBasedMitigations" whose(it as string as version = "1.0") of keys "HKLM\SOFTWARE\Mic` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0377` `not exists (it as integer) whose(it = 1) of values "AutoBackupLogFiles" of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Mi` The operator "keys" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0379` `not exists keys whose(value "DisplayName" of it as string starts with "Adobe Digital Editions") of keys "HKEY_LOCAL_MACH` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0380` `x64 of operating system` The operator "x64" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0381` `not exists services whose(display name of it starts with "GitHub Actions Runner")` The operator "services" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0382` `not exists keys whose(value "DisplayName" of it as string contains "Node.js" AND value "DisplayVersion" of it as string ` The operator "x32 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0384` `not exists maxima whose(it >= "13.0.16100.1") of (it as string as version) of values "DisplayVersion" of keys whose(valu` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0389` `exists values "server" of keys whose(name of it as lowercase contains "bfenterprise") of keys "HKEY_LOCAL_MACHINE\SOFTWA` The operator "native registry" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0391` `exists string values whose(it contains "PG279Q") of selects "MonitorType from Win32_DesktopMonitor" of wmis` The operator "wmis" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0398` `not exists distinguished names of local computers of active directories` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0400` `not exists (now - it) whose(it < 3 * day) of maxima of modification times of files whose(name of it as lowercase contain` The operator "windows folders" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0402` `not exists values "BESConsole.exe" whose(it as string as integer = 1) of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Int` The operator "native registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0405` `not exists start types whose(it = "demand") of services "tzautoupdate"` The operator "start types" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0406` `exists keys whose (value "DisplayName" of it as string contains "GoToMeeting") of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Micr` The operator "x64 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0407` `exists keys whose(value "DisplayName" of it as string starts with "Zoom VDI Workplace" AND value "DisplayVersion" of it ` The operator "x32 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0409` `exists keys whose(value "DisplayName" of it as string starts with "NVIDIA Graphics Driver") of keys "HKLM\Software\Micro` The operator "x32 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0410` `not exists (it as string as version) whose(it >= "511.79") of values "DisplayVersion" of keys whose(value "DisplayName" ` The operator "x32 registries" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0411` `/* exists NVIDIA GPU */ exists active devices whose(manufacturer of it as uppercase is "NVIDIA" AND description of it co` The operator "active devices" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0413` `exists (user masks of it; group masks of it; other masks of it) whose(it < 6) of folders "/tmp"` The operator "user masks" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0415` `exists (packages "docker" of it; packages "docker-engine" of it; packages "docker.io" of it) of debianpackages` The operator "debianpackages" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0523` `version of registration server >= "9.5.0"` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0527` `exists versions whose(it >= "9.2") of relay services` The operator "versions" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0532` `version of registration server >= "8.1"` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0045` `exists key "HKLM\SOFTWARE" of native registry` The operator "native registry" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0073` `(windows of operating systems) AND (not exists distinguished names of local computers of active directories) AND (comput` The operator "active directories" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0080` `(windows of operating systems) AND (not exists distinguished names of local computers of active directories)` The operator "active directories" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0091` `exists files ending in ".png.base64" of folders "/tmp/_BigFix/Icons"` The operator "files ending in" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0092` `number of files ending in ".png.base64" of folders "/tmp/_BigFix/Icons"` The operator "files ending in" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0115` `average duration of evaluationcycle of client` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0147` `( exists lengths whose(it > 1) of (it as trimmed string) of values of settings "_BESServer_Database_Server" of client ) ` The operator "registry" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0162` `exists cloud providers` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0179` `exists keys whose(value "DisplayName" of it as string as trimmed string starts with "Dell Command | Update" AND value "D` The operator "registry" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0218` `exists wmi "root/cimv2/mdm/dmmap"` The operator "wmi" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0219` `exists selects "InstanceID from MDM_RemoteFind" of wmis "root/cimv2/mdm/dmmap"` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0220` `(exists selects "InstanceID from MDM_RemoteFind" of wmis "root/cimv2/mdm/dmmap") as string` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0221` `(concatenation ", " of (it as string) of integer values of selects "DesiredAccuracy from MDM_RemoteFind" of wmis "root/c` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0222` `(concatenation ", " of (it as string) of integer values of selects "MaximumAge from MDM_RemoteFind" of wmis "root/cimv2/` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0223` `(concatenation ", " of (it as string) of integer values of selects "Timeout from MDM_RemoteFind" of wmis "root/cimv2/mdm` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0224` `(exists selects "InstanceID from MDM_RemoteFind_Location01" of wmis "root/cimv2/mdm/dmmap") as string | "N/A"` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0225` `(concatenation ", " of (it as string) of string values of selects "Latitude from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0226` `(concatenation ", " of (it as string) of string values of selects "Longitude from MDM_RemoteFind_Location01" of wmis "ro` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0227` `(concatenation ", " of (it as string) of string values of selects "Altitude from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0228` `(concatenation ", " of (it as string) of string values of selects "Accuracy from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0229` `(concatenation ", " of (it as string) of string values of selects "AltitudeAccuracy from MDM_RemoteFind_Location01" of w` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0230` `(concatenation ", " of (it as string) of string values of selects "Age from MDM_RemoteFind_Location01" of wmis "root/cim` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0245` `exists keys "HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" of (x` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0304` `exists string values of selects "DeviceHardwareData from MDM_DevDetail_Ext01 where InstanceID='Ext' AND ParentID='./DevD` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0344` `exists debianpackages` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0345` `not exists packages "docker-ce" of debianpackages` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0346` `(((unix of operating system) AND (exists debianpackages)) AND (not exists packages "docker-ce" of debianpackages)) AND (` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0347` `((unix of operating system) AND (exists debianpackages)) AND (4 != number of (packages "apt-transport-https" of it; pack` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0348` `((((unix of operating system) AND (exists packages "apt" of debianpackages)) AND (exists packages "curl" of debianpackag` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0349` `((((unix of operating system) AND (exists architectures whose(it = "x86_64") of operating systems)) AND (exists packages` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0350` `(((unix of operating system) AND (exists packages "apt" of debianpackages)) AND (exists lines whose(it does not start wi` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0351` `exists dns domainnames of local computers of active directories` The operator "active directories" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0352` `exists (it as trimmed string) whose(it != "") of substrings separated by "," of (it as string as lowercase) of values "S` The operator "registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0353` `not exists (it as trimmed string) whose(it contains unique value of dns domainnames of local computers of active directo` The operator "registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0355` `exists packages "apt" of debianpackages` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0371` `not exists lines whose(it contains "@type %25*") of files "cat.bat" of windows folders` The operator "windows folders" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0372` `not exists lines whose(it contains "@dir %25*") of files "ls.bat" of windows folders` The operator "windows folders" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0373` `not exists values "{CA8A9780-280D-11CF-A24D-444553540000}" whose(it as string as integer = 0) of keys "HKEY_LOCAL_MACHIN` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0374` `exists keys whose( exists values "DisplayName" whose(it as string as lowercase contains "Acrobat Reader" as lowercase) o` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0375` `/* I think this is only needed if Hyper-V is in use */ exists keys "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Vi` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0376` `not exists values "MinVmVersionForCpuBasedMitigations" whose(it as string as version = "1.0") of keys "HKLM\SOFTWARE\Mic` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0377` `not exists (it as integer) whose(it = 1) of values "AutoBackupLogFiles" of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Mi` The operator "registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0379` `not exists keys whose(value "DisplayName" of it as string starts with "Adobe Digital Editions") of keys "HKEY_LOCAL_MACH` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0380` `x64 of operating system` The operator "x64" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0381` `not exists services whose(display name of it starts with "GitHub Actions Runner")` The operator "display name" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0382` `not exists keys whose(value "DisplayName" of it as string contains "Node.js" AND value "DisplayVersion" of it as string ` The operator "x32 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0384` `not exists maxima whose(it >= "13.0.16100.1") of (it as string as version) of values "DisplayVersion" of keys whose(valu` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0389` `exists values "server" of keys whose(name of it as lowercase contains "bfenterprise") of keys "HKEY_LOCAL_MACHINE\SOFTWA` The operator "registry" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0391` `exists string values whose(it contains "PG279Q") of selects "MonitorType from Win32_DesktopMonitor" of wmis` The operator "wmis" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0398` `not exists distinguished names of local computers of active directories` The operator "active directories" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0400` `not exists (now - it) whose(it < 3 * day) of maxima of modification times of files whose(name of it as lowercase contain` The operator "windows folders" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0401` `exists regapps "BESConsole.exe"` The operator "regapps" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0402` `not exists values "BESConsole.exe" whose(it as string as integer = 1) of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Int` The operator "native registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0405` `not exists start types whose(it = "demand") of services "tzautoupdate"` The operator "start types" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0406` `exists keys whose (value "DisplayName" of it as string contains "GoToMeeting") of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Micr` The operator "x64 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0407` `exists keys whose(value "DisplayName" of it as string starts with "Zoom VDI Workplace" AND value "DisplayVersion" of it ` The operator "x32 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0409` `exists keys whose(value "DisplayName" of it as string starts with "NVIDIA Graphics Driver") of keys "HKLM\Software\Micro` The operator "x32 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0410` `not exists (it as string as version) whose(it >= "511.79") of values "DisplayVersion" of keys whose(value "DisplayName" ` The operator "x32 registries" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0411` `/* exists NVIDIA GPU */ exists active devices whose(manufacturer of it as uppercase is "NVIDIA" AND description of it co` The operator "active devices" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0415` `exists (packages "docker" of it; packages "docker-engine" of it; packages "docker.io" of it) of debianpackages` The operator "debianpackages" is not defined.
- Rocky Linux 9.3 arm64 (container): error `r0523` `version of registration server >= "9.5.0"` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0532` `version of registration server >= "8.1"` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0045` `exists key "HKLM\SOFTWARE" of native registry` The operator "native registry" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0073` `(windows of operating systems) AND (not exists distinguished names of local computers of active directories) AND (comput` The operator "active directories" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0080` `(windows of operating systems) AND (not exists distinguished names of local computers of active directories)` The operator "active directories" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0091` `exists files ending in ".png.base64" of folders "/tmp/_BigFix/Icons"` The operator "files ending in" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0092` `number of files ending in ".png.base64" of folders "/tmp/_BigFix/Icons"` The operator "files ending in" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0115` `average duration of evaluationcycle of client` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0147` `( exists lengths whose(it > 1) of (it as trimmed string) of values of settings "_BESServer_Database_Server" of client ) ` The operator "registry" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0162` `exists cloud providers` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0179` `exists keys whose(value "DisplayName" of it as string as trimmed string starts with "Dell Command | Update" AND value "D` The operator "registry" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0218` `exists wmi "root/cimv2/mdm/dmmap"` The operator "wmi" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0219` `exists selects "InstanceID from MDM_RemoteFind" of wmis "root/cimv2/mdm/dmmap"` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0220` `(exists selects "InstanceID from MDM_RemoteFind" of wmis "root/cimv2/mdm/dmmap") as string` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0221` `(concatenation ", " of (it as string) of integer values of selects "DesiredAccuracy from MDM_RemoteFind" of wmis "root/c` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0222` `(concatenation ", " of (it as string) of integer values of selects "MaximumAge from MDM_RemoteFind" of wmis "root/cimv2/` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0223` `(concatenation ", " of (it as string) of integer values of selects "Timeout from MDM_RemoteFind" of wmis "root/cimv2/mdm` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0224` `(exists selects "InstanceID from MDM_RemoteFind_Location01" of wmis "root/cimv2/mdm/dmmap") as string | "N/A"` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0225` `(concatenation ", " of (it as string) of string values of selects "Latitude from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0226` `(concatenation ", " of (it as string) of string values of selects "Longitude from MDM_RemoteFind_Location01" of wmis "ro` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0227` `(concatenation ", " of (it as string) of string values of selects "Altitude from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0228` `(concatenation ", " of (it as string) of string values of selects "Accuracy from MDM_RemoteFind_Location01" of wmis "roo` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0229` `(concatenation ", " of (it as string) of string values of selects "AltitudeAccuracy from MDM_RemoteFind_Location01" of w` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0230` `(concatenation ", " of (it as string) of string values of selects "Age from MDM_RemoteFind_Location01" of wmis "root/cim` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0245` `exists keys "HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending" of (x` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0304` `exists string values of selects "DeviceHardwareData from MDM_DevDetail_Ext01 where InstanceID='Ext' AND ParentID='./DevD` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0351` `exists dns domainnames of local computers of active directories` The operator "active directories" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0352` `exists (it as trimmed string) whose(it != "") of substrings separated by "," of (it as string as lowercase) of values "S` The operator "registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0353` `not exists (it as trimmed string) whose(it contains unique value of dns domainnames of local computers of active directo` The operator "registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0371` `not exists lines whose(it contains "@type %25*") of files "cat.bat" of windows folders` The operator "windows folders" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0372` `not exists lines whose(it contains "@dir %25*") of files "ls.bat" of windows folders` The operator "windows folders" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0373` `not exists values "{CA8A9780-280D-11CF-A24D-444553540000}" whose(it as string as integer = 0) of keys "HKEY_LOCAL_MACHIN` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0374` `exists keys whose( exists values "DisplayName" whose(it as string as lowercase contains "Acrobat Reader" as lowercase) o` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0375` `/* I think this is only needed if Hyper-V is in use */ exists keys "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Vi` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0376` `not exists values "MinVmVersionForCpuBasedMitigations" whose(it as string as version = "1.0") of keys "HKLM\SOFTWARE\Mic` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0377` `not exists (it as integer) whose(it = 1) of values "AutoBackupLogFiles" of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Mi` The operator "registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0379` `not exists keys whose(value "DisplayName" of it as string starts with "Adobe Digital Editions") of keys "HKEY_LOCAL_MACH` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0380` `x64 of operating system` The operator "x64" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0381` `not exists services whose(display name of it starts with "GitHub Actions Runner")` The operator "services" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0382` `not exists keys whose(value "DisplayName" of it as string contains "Node.js" AND value "DisplayVersion" of it as string ` The operator "x32 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0384` `not exists maxima whose(it >= "13.0.16100.1") of (it as string as version) of values "DisplayVersion" of keys whose(valu` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0389` `exists values "server" of keys whose(name of it as lowercase contains "bfenterprise") of keys "HKEY_LOCAL_MACHINE\SOFTWA` The operator "registry" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0391` `exists string values whose(it contains "PG279Q") of selects "MonitorType from Win32_DesktopMonitor" of wmis` The operator "wmis" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0398` `not exists distinguished names of local computers of active directories` The operator "active directories" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0400` `not exists (now - it) whose(it < 3 * day) of maxima of modification times of files whose(name of it as lowercase contain` The operator "windows folders" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0401` `exists regapps "BESConsole.exe"` The operator "regapps" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0402` `not exists values "BESConsole.exe" whose(it as string as integer = 1) of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Int` The operator "native registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0405` `not exists start types whose(it = "demand") of services "tzautoupdate"` The operator "start types" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0406` `exists keys whose (value "DisplayName" of it as string contains "GoToMeeting") of keys "HKEY_LOCAL_MACHINE\SOFTWARE\Micr` The operator "x64 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0407` `exists keys whose(value "DisplayName" of it as string starts with "Zoom VDI Workplace" AND value "DisplayVersion" of it ` The operator "x32 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0409` `exists keys whose(value "DisplayName" of it as string starts with "NVIDIA Graphics Driver") of keys "HKLM\Software\Micro` The operator "x32 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0410` `not exists (it as string as version) whose(it >= "511.79") of values "DisplayVersion" of keys whose(value "DisplayName" ` The operator "x32 registries" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0411` `/* exists NVIDIA GPU */ exists active devices whose(manufacturer of it as uppercase is "NVIDIA" AND description of it co` The operator "active devices" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0523` `version of registration server >= "9.5.0"` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0532` `version of registration server >= "8.1"` No inspector context.

## False "safe": classifier (platform known) (17)

- macOS 15.7 x86_64 (SSH, installed agent): error `r0083` `exists unique values whose(it starts with "Hyper-V") of ( ( if ( exists (it as string) whose(it contains "<dmi>") of pro` The operator "version strings" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0115` `average duration of evaluationcycle of client` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0162` `exists cloud providers` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0351` `exists dns domainnames of local computers of active directories` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0381` `not exists services whose(display name of it starts with "GitHub Actions Runner")` The operator "services" is not defined.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0398` `not exists distinguished names of local computers of active directories` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0523` `version of registration server >= "9.5.0"` No inspector context.
- macOS 15.7 x86_64 (SSH, installed agent): error `r0532` `version of registration server >= "8.1"` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0115` `average duration of evaluationcycle of client` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0162` `exists cloud providers` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0523` `version of registration server >= "9.5.0"` No inspector context.
- Rocky Linux 9.3 arm64 (container): error `r0532` `version of registration server >= "8.1"` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0115` `average duration of evaluationcycle of client` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0162` `exists cloud providers` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0381` `not exists services whose(display name of it starts with "GitHub Actions Runner")` The operator "services" is not defined.
- Ubuntu 26.04 arm64 (container): error `r0523` `version of registration server >= "9.5.0"` No inspector context.
- Ubuntu 26.04 arm64 (container): error `r0532` `version of registration server >= "8.1"` No inspector context.
