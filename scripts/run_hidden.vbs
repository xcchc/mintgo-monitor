' Run scripts\run.bat with no visible window.
'
' Why: the scheduled task runs as the interactive user, so invoking cmd.exe
' directly flashes a console window every interval. WScript.Shell.Run with
' window style 0 starts the batch hidden; child processes inherit the hidden
' console, so no window pops up either.
'
' Keep this file ASCII-only.

Dim fso, sh, bat
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
bat = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "run.bat")
sh.Run """" & bat & """", 0, False
