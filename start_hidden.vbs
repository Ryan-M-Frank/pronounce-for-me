' Launches pronounce-for-me with no console window.
' Double-click this, or drop a shortcut to it in
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
' to have it running every time you log in.
'
' Uses the portable Python in .\python\ if present, otherwise pythonw.exe on PATH.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

embedded = fso.BuildPath(base, "python\pythonw.exe")
If fso.FileExists(embedded) Then
    exe = embedded
Else
    exe = "pythonw.exe"
End If

script = fso.BuildPath(base, "pronounce_for_me.py")
sh.Run """" & exe & """ """ & script & """", 0, False
