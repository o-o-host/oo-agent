; Inno Setup script for the oo-agent Windows installer.
;
; Build the exes first (deploy\windows\build.ps1), then compile this
; script with Inno Setup 6:  iscc deploy\windows\oo-agent.iss
;
; The installer registers and starts the "oo-agent" Windows service
; (LocalSystem). Full CPU sensor coverage additionally requires the
; PawnIO driver (https://pawnio.eu/) — the agent degrades gracefully
; without it (board/disk/GPU sensors still work).

#define AppName "oo-agent"
#define AppVersion "0.1.0"
#define AppPublisher "o-o.host"

[Setup]
AppId={{7A3F1C7E-1B0B-4A56-9E9C-0A6F00A6E217}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=oo-agent-setup-{#AppVersion}
OutputDir=..\..\dist
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\oo-agent.exe

[Files]
Source: "..\..\dist\oo-agent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\oo-agent-service.exe"; DestDir: "{app}"; Flags: ignoreversion
; Default config: never overwrite an existing one.
Source: "..\..\agent.ini.example"; DestDir: "{commonappdata}\{#AppName}"; \
    DestName: "agent.ini"; Flags: onlyifdoesntexist

[Dirs]
Name: "{commonappdata}\{#AppName}"; Permissions: admins-full system-full
Name: "{commonappdata}\{#AppName}\plugins"

[Run]
Filename: "{app}\oo-agent-service.exe"; Parameters: "--startup auto install"; \
    Flags: runhidden; StatusMsg: "Registering the oo-agent service..."
Filename: "{app}\oo-agent-service.exe"; Parameters: "start"; \
    Flags: runhidden; StatusMsg: "Starting the oo-agent service..."

[UninstallRun]
Filename: "{app}\oo-agent-service.exe"; Parameters: "stop"; \
    Flags: runhidden; RunOnceId: "StopService"
Filename: "{app}\oo-agent-service.exe"; Parameters: "remove"; \
    Flags: runhidden; RunOnceId: "RemoveService"

[UninstallDelete]
; Keep agent.ini and the enroll token; drop only the offline queue.
Type: files; Name: "{commonappdata}\{#AppName}\queue.db"
